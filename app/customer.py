"""Customer session core: signaling + WebRTC streaming + authorized input.

This module is deliberately UI-free so it can run headless and be tested
end-to-end.  app.customer_gui.Tk app (customer_app.py) drives it from Tk.

State machine (reported through the callbacks object):

    IDLE -> CONNECTING -> WAITING -> CONSENT -> CONNECTING_SESSION -> ACTIVE
                                                          |            |
                                                          v            v
                                                        ERROR        ENDED

Consent rules:
  * Nothing is captured until the customer explicitly allows screen viewing.
  * Mouse/keyboard events are ignored unless the customer allowed control.
  * Any state can end immediately via end_session() (customer's End button,
    technician's End button, network failure, ...).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from . import protocol
from .capture import ScreenCapturer
from .displayutil import DisplayError
from .inputctl import InputController
from .signaling import SignalingClient, SignalingError

log = logging.getLogger("support.customer")


class CustomerState(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    WAITING = "waiting"
    CONSENT = "consent"
    CONNECTING_SESSION = "connecting-session"
    ACTIVE = "active"
    ENDED = "ended"
    ERROR = "error"


@dataclass
class CustomerConfig:
    signaling_url: str
    code: str
    display: str
    fps: float = 8.0
    max_width: int = 0             # 0 = never downscale (send the desktop 1:1)
    capture_backend: str = "auto"  # auto | pipewire | mss | ffmpeg
    ice_servers: Optional[list] = None  # overrides server-provided list
    connect_timeout: float = 25.0      # WebRTC establishment watchdog
    media_timeout: float = 15.0        # signaling link unavailable after start


class CustomerCallbacks:
    """Override the methods you care about.  Called from the session thread."""

    def on_state(self, state: CustomerState, message: str = "") -> None:
        pass

    def on_technician_present(self) -> None:
        pass

    def on_session_ended(self, reason: str) -> None:
        pass

    def on_error(self, message: str) -> None:
        pass


class ScreenVideoTrack:
    """aiortc VideoStreamTrack fed by ScreenCapturer (created lazily)."""

    _cls_cache: Optional[type] = None

    @staticmethod
    def _track_class():
        if ScreenVideoTrack._cls_cache is None:
            import av
            from aiortc import VideoStreamTrack

            class _ScreenTrack(VideoStreamTrack):
                kind = "video"

                def __init__(self, capturer: ScreenCapturer, fps: float, max_width: int):
                    super().__init__()
                    self.capturer = capturer
                    self.interval = 1.0 / max(1.0, fps)
                    self.max_width = max_width
                    self._t0: Optional[float] = None
                    self._elapsed = 0.0

                async def recv(self) -> "av.VideoFrame":
                    from fractions import Fraction

                    if self._t0 is None:
                        self._t0 = time.monotonic()
                    else:
                        self._elapsed += self.interval
                        wait = self._t0 + self._elapsed - time.monotonic()
                        if wait > 0:
                            await asyncio.sleep(wait)
                    pts = int(self._elapsed * 90000)

                    data, geom, _seq = self.capturer.latest()
                    w, h = geom["width"], geom["height"]
                    if not data or w <= 0 or h <= 0:
                        # Capture not ready yet: emit a black frame to keep the
                        # encoder clock running instead of stalling.
                        w, h = max(w, 2), max(h, 2)
                        data = b"\x00" * (w * h * 4)

                    frame = av.VideoFrame(width=w, height=h, format="bgra")
                    frame.planes[0].update(data)
                    if self.max_width and w > self.max_width:
                        new_w = self.max_width
                        new_h = max(2, int(h * (new_w / w)) // 2 * 2)
                        frame = av.video.reformatter.VideoReformatter().reformat(
                            frame, width=new_w, height=new_h, format="bgra"
                        )
                    frame = frame.reformat(format="yuv420p")
                    frame.pts = pts
                    frame.time_base = Fraction(1, 90000)
                    return frame

            ScreenVideoTrack._cls_cache = _ScreenTrack
        return ScreenVideoTrack._cls_cache

    @staticmethod
    def create(capturer: ScreenCapturer, fps: float, max_width: int):
        return ScreenVideoTrack._track_class()(capturer, fps, max_width)


def to_rtc_ice_servers(ice_list: Optional[list]) -> list:
    """Convert [{urls,username,credential}, ...] to aiortc RTCIceServer list."""
    from aiortc import RTCIceServer

    servers = []
    for item in ice_list or []:
        if isinstance(item, str):
            servers.append(RTCIceServer(urls=item))
        elif isinstance(item, dict) and item.get("urls"):
            servers.append(
                RTCIceServer(
                    urls=item["urls"],
                    username=item.get("username"),
                    credential=item.get("credential"),
                )
            )
    return servers


class CustomerSession:
    def __init__(
        self,
        config: CustomerConfig,
        callbacks: Optional[CustomerCallbacks] = None,
    ) -> None:
        self.config = config
        self.cb = callbacks or CustomerCallbacks()
        self.state = CustomerState.IDLE

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread = None
        self._signaling: Optional[SignalingClient] = None
        self._pc = None
        self._input: Optional[InputController] = None
        self._capturer: Optional[ScreenCapturer] = None
        self._track = None
        self._ice_servers: list = []
        self._peer_present = False
        self._consent_view = False
        self._consent_control = False
        self._connected = False
        self._close_reason = "session ended"
        self._ending = False
        self._main_task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------- control

    def start_background(self) -> None:
        """Run the session on a dedicated event loop thread."""
        import threading

        def runner() -> None:
            asyncio.run(self.run())

        self._thread = threading.Thread(target=runner, name="customer-session", daemon=True)
        self._thread.start()

    def wait_started(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._loop is not None:
                return True
            time.sleep(0.02)
        return self._loop is not None

    def stop(self, reason: str = "ended by customer") -> None:
        self._schedule(self.end_session(reason))

    def grant_consent(self, view: bool, control: bool) -> None:
        self._schedule(self._do_grant_consent(view, control))

    def deny(self) -> None:
        self.stop("customer declined access")

    def _schedule(self, coro) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, loop)

    # -------------------------------------------------------------------- run

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        try:
            await self._main()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # never let the session die silently
            log.exception("customer session crashed")
            self._set_state(CustomerState.ERROR, str(exc))
            self.cb.on_error(f"unexpected error: {exc}")
        finally:
            await self._cleanup()

    async def _main(self) -> None:
        cfg = self.config
        self._set_state(CustomerState.CONNECTING, "connecting to support")
        self._signaling = SignalingClient(
            url=cfg.signaling_url,
            role=protocol.ROLE_CUSTOMER,
            code=cfg.code,
            on_message=self._on_signal_message,
            on_state=self._on_signaling_state,
        )
        self._signaling.auto_reconnect = True
        # Watchdog: if we never get a welcome, fail with a real error.
        watchdog = asyncio.create_task(self._startup_watchdog(cfg.media_timeout))
        await self._signaling.run()
        watchdog.cancel()
        if self.state not in (CustomerState.ENDED, CustomerState.ERROR):
            self._set_state(CustomerState.ENDED, "signaling connection closed")

    async def _startup_watchdog(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.state in (CustomerState.CONNECTING,) and not self._connected:
            await self.end_session(
                f"could not reach the support server at "
                f"{self.config.signaling_url} within {delay:.0f}s",
                failed=True,
            )

    # ------------------------------------------------------------- signaling

    async def _on_signaling_state(self, message: str) -> None:
        log.info("signaling state: %s", message)

    async def _on_peer_available(self) -> None:
        """A technician is paired with this session (idempotent)."""
        self._peer_present = True
        self._signaling.auto_reconnect = False
        if self.state in (CustomerState.WAITING, CustomerState.CONNECTING):
            self._set_state(CustomerState.CONSENT, "technician is ready")
            self.cb.on_technician_present()

    async def _on_signal_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == protocol.MSG_WELCOME:
            ice = msg.get("ice_servers")
            if self.config.ice_servers is not None:
                ice = self.config.ice_servers
            self._ice_servers = ice or []
            self._connected = True
            if msg.get("peer") == protocol.ROLE_TECHNICIAN:
                self._peer_present = True
                self._signaling.auto_reconnect = False
                self._set_state(CustomerState.CONSENT, "technician is ready")
                self.cb.on_technician_present()
            else:
                self._set_state(CustomerState.WAITING, "waiting for technician")
        elif mtype == protocol.MSG_PEER_JOINED:
            await self._on_peer_available()
        elif mtype == protocol.MSG_PONG:
            # Self-healing: a pong that reports a paired technician recovers
            # from a lost "peer-joined" notification (keepalive pings repeat).
            if msg.get("peer") == protocol.ROLE_TECHNICIAN:
                await self._on_peer_available()
        elif mtype == protocol.MSG_PEER_LEFT:
            self._peer_present = False
            if self._connected and self.state == CustomerState.ACTIVE:
                await self.end_session("technician ended the session")
            elif self.state in (CustomerState.CONSENT, CustomerState.CONNECTING_SESSION):
                self._set_state(CustomerState.WAITING, "technician left")
            # While WAITING: keep waiting (auto-reconnect keeps the link alive).
        elif mtype == protocol.MSG_SIGNAL:
            await self._on_rtc_signal(msg.get("kind"), msg.get("payload") or {})
        elif mtype == protocol.MSG_ERROR:
            message = msg.get("message", "server error")
            if msg.get("code") in (
                protocol.ERR_BAD_CODE, protocol.ERR_BAD_TOKEN, protocol.ERR_SESSION_LIMIT
            ):
                await self.end_session(message, failed=True)
            else:
                self.cb.on_error(message)
        elif mtype == protocol.MSG_CONSENT:
            pass  # customer is the consent source, not the consumer

    async def _on_rtc_signal(self, kind: str, payload: dict) -> None:
        pc = self._pc
        if pc is None:
            return
        try:
            if kind == protocol.SIGNAL_ANSWER:
                from aiortc import RTCSessionDescription

                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=payload.get("sdp", ""), type="answer")
                )
            elif kind == protocol.SIGNAL_ICE:
                cand = payload.get("candidate")
                if not cand:
                    await pc.addIceCandidate(None)
                    return
                from aiortc.sdp import candidate_from_sdp

                ice = candidate_from_sdp(cand)
                ice.sdpMid = payload.get("sdpMid")
                ice.sdpMLineIndex = payload.get("sdpMLineIndex")
                await pc.addIceCandidate(ice)
        except Exception as exc:
            log.warning("failed to apply %s: %s", kind, exc)

    # --------------------------------------------------------------- consent

    async def _do_grant_consent(self, view: bool, control: bool) -> None:
        if self.state not in (CustomerState.CONSENT, CustomerState.WAITING):
            return
        if not view and not control:
            await self.end_session("customer declined access")
            return
        self._consent_view = bool(view)
        self._consent_control = bool(control)
        # Tell the technician exactly what was authorized (and enforce it here
        # again on every input event).
        try:
            await self._signaling.send_consent(self._consent_view, self._consent_control)
        except Exception as exc:
            log.warning("could not send consent state: %s", exc)
        try:
            await self._start_webrtc()
        except Exception as exc:
            log.exception("failed to start WebRTC session")
            await self.end_session(f"failed to start session: {exc}", failed=True)

    async def _start_webrtc(self) -> None:
        from aiortc import RTCConfiguration, RTCPeerConnection

        cfg = self.config
        self._set_state(CustomerState.CONNECTING_SESSION, "connecting to technician")

        if self._consent_control:
            self._input = InputController(cfg.display)

        self._pc = pc = RTCPeerConnection(
            configuration=RTCConfiguration(
                iceServers=to_rtc_ice_servers(self._ice_servers)
            )
        )

        @pc.on("connectionstatechange")
        async def on_state_change() -> None:
            state = pc.connectionState
            log.info("webrtc connection state: %s", state)
            if state == "connected":
                self._connected = True
                self._set_state(CustomerState.ACTIVE, "session active")
            elif state in ("failed", "closed"):
                # Run teardown as a separate task: closing the peer connection
                # from inside its own state-change callback can deadlock.
                asyncio.create_task(
                    self.end_session("connection to technician lost", failed=True)
                )
            elif state == "disconnected":
                async def grace() -> None:
                    await asyncio.sleep(10)
                    if pc.connectionState == "disconnected":
                        asyncio.create_task(
                            self.end_session("connection to technician lost",
                                             failed=True)
                        )

                asyncio.create_task(grace())

        if self._consent_view:
            self._capturer = ScreenCapturer(
                cfg.display, fps=cfg.fps, backend=cfg.capture_backend
            )
            self._capturer.prepare()
            self._capturer.start()
            self._track = ScreenVideoTrack.create(self._capturer, cfg.fps, cfg.max_width)
            pc.addTrack(self._track)

        channel = pc.createDataChannel("input", ordered=True)

        @channel.on("message")
        def on_input_message(message) -> None:
            if not self._consent_control or self._input is None:
                return  # control was not authorized
            try:
                data = json.loads(message) if isinstance(message, str) else None
            except ValueError:
                return
            if not isinstance(data, dict):
                return
            try:
                self._apply_input(data)
            except Exception as exc:
                log.warning("input event failed: %s", exc)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await self._signaling.send_signal(
            protocol.SIGNAL_OFFER,
            {"type": "offer", "sdp": pc.localDescription.sdp},
        )
        # WebRTC establishment watchdog.
        asyncio.create_task(self._webrtc_watchdog(cfg.connect_timeout))

    async def _webrtc_watchdog(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.state == CustomerState.CONNECTING_SESSION:
            await self.end_session(
                "could not establish a connection to the technician within "
                f"{delay:.0f}s (network may block peer-to-peer traffic; a TURN "
                "relay may be required)",
                failed=True,
            )

    # ----------------------------------------------------------------- input

    def _apply_input(self, data: dict) -> None:
        """Map normalized technician coordinates to this desktop and inject."""
        assert self._input is not None
        geom = self._capturer.geometry() if self._capturer else self._screen_geometry()
        itype = data.get("type")
        if itype == "move":
            x, y = self._map_point(data.get("x"), data.get("y"), geom)
            self._input.pointer_move(x, y)
        elif itype == "button":
            if "x" in data and "y" in data:
                x, y = self._map_point(data.get("x"), data.get("y"), geom)
                self._input.pointer_move(x, y)
            self._input.button(str(data.get("button", "left")), bool(data.get("down")))
        elif itype == "scroll":
            self._input.scroll(float(data.get("dx", 0)), float(data.get("dy", 0)))
        elif itype == "key":
            self._input.key(str(data.get("key", "")), bool(data.get("down")))

    def _screen_geometry(self) -> dict:
        from .displayutil import verify_display

        info = verify_display(self.config.display)
        return {"left": 0, "top": 0, "width": info["width"], "height": info["height"]}

    @staticmethod
    def _map_point(x01: Any, y01: Any, geom: dict) -> tuple[int, int]:
        """Normalized video coordinates -> absolute root-window coordinates."""
        try:
            x01 = max(0.0, min(1.0, float(x01)))
            y01 = max(0.0, min(1.0, float(y01)))
        except (TypeError, ValueError):
            raise ValueError("invalid coordinates")
        w, h = int(geom["width"]), int(geom["height"])
        x = int(geom["left"] + round(x01 * (w - 1)))
        y = int(geom["top"] + round(y01 * (h - 1)))
        return x, y

    # ------------------------------------------------------------- lifecycle

    async def end_session(self, reason: str = "session ended", failed: bool = False) -> None:
        if self.state in (CustomerState.ENDED, CustomerState.ERROR):
            return
        if self._ending:
            return
        self._ending = True
        self._close_reason = reason
        self._set_state(CustomerState.ERROR if failed else CustomerState.ENDED, reason)
        # 1) Revoke remote access immediately (capture + input), bounded.
        try:
            await asyncio.wait_for(self._cleanup(), timeout=8)
        except asyncio.TimeoutError:
            log.warning("session cleanup timed out")
        # 2) Tell the application layer the session is over.
        self.cb.on_session_ended(reason)
        if failed:
            self.cb.on_error(reason)
        # 3) Best-effort signaling teardown, bounded so nothing can hang here.
        if self._signaling is not None:
            try:
                await asyncio.wait_for(self._signaling.leave(), timeout=3)
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._signaling.close(), timeout=3)
            except Exception:
                pass

    async def _cleanup(self) -> None:
        # Stop sharing and control first; closing the peer connection last.
        if self._track is not None:
            try:
                self._track.stop()
            except Exception:
                pass
            self._track = None
        if self._capturer is not None:
            self._capturer.stop()
            self._capturer = None
        if self._input is not None:
            try:
                self._input.close()
            except Exception:
                pass
            self._input = None
        pc, self._pc = self._pc, None
        if pc is not None:
            try:
                await asyncio.wait_for(pc.close(), timeout=5)
            except Exception:
                pass

    def _set_state(self, state: CustomerState, message: str = "") -> None:
        self.state = state
        try:
            self.cb.on_state(state, message)
        except Exception:
            log.exception("state callback failed")
