"""End-to-end session tests: real WebRTC between the customer core and a
technician peer over the real signaling server, on a real virtual X display.

This exercises the exact production code paths used across two computers:
signaling handshake, SDP/ICE exchange, VP8 screen streaming, the input data
channel with consent enforcement, and session teardown.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from aiohttp import ClientSession, WSMsgType

from app.customer import (
    CustomerCallbacks,
    CustomerConfig,
    CustomerSession,
    CustomerState,
)
from app.inputctl import InputController, query_pointer
from tests.conftest import TECH_TOKEN, draw_window

FULL_GEOM = {"left": 0, "top": 0, "width": 1280, "height": 800}


class Recorder(CustomerCallbacks):
    def __init__(self, consent: tuple[bool, bool] | None = None):
        self.states: list[tuple[CustomerState, str]] = []
        self.errors: list[str] = []
        self.ended: list[str] = []
        self.consent = consent
        self.session: CustomerSession | None = None
        self.technician_arrived = threading_event()

    def on_state(self, state, message=""):
        self.states.append((state, message))

    def on_technician_present(self):
        self.technician_arrived.set()
        if self.consent is not None and self.session is not None:
            self.session.grant_consent(*self.consent)

    def on_session_ended(self, reason):
        self.ended.append(reason)

    def on_error(self, message):
        self.errors.append(message)


def threading_event():
    import threading
    return threading.Event()


class TechPeer:
    """A minimal technician: WebRTC peer written with aiortc (like the browser)."""

    def __init__(self, ws_url: str, code: str = "1234"):
        self.ws_url = ws_url
        self.code = code
        self.pc = None
        self.channel = None
        self.frames: list = []
        self.states: list[str] = []
        self._session: ClientSession | None = None
        self._ws = None

    async def run(self) -> None:
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
        from aiortc.sdp import candidate_from_sdp

        self._session = ClientSession()
        self._ws = await self._session.ws_connect(self.ws_url, timeout=5)
        await self._ws.send_json({
            "type": "hello", "role": "technician",
            "code": self.code, "token": TECH_TOKEN,
        })

        async def collect(track):
            try:
                while True:
                    frame = await track.recv()
                    self.frames.append(frame)
            except Exception:
                pass

        async def handle(msg: dict) -> bool:
            mtype = msg.get("type")
            if mtype == "welcome":
                self.pc = pc = RTCPeerConnection(
                    configuration=RTCConfiguration(iceServers=[])
                )
                pc.on("track", lambda t: asyncio.ensure_future(collect(t)))
                pc.on("datachannel", lambda ch: setattr(self, "channel", ch)
                      if ch.label == "input" else None)
                pc.on("connectionstatechange",
                      lambda: self.states.append(pc.connectionState))
            elif mtype == "signal" and msg.get("kind") == "offer":
                pc = self.pc
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg["payload"]["sdp"], type="offer")
                )
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await self._ws.send_json({
                    "type": "signal", "kind": "answer",
                    "payload": {"type": "answer", "sdp": pc.localDescription.sdp},
                })
            elif mtype == "signal" and msg.get("kind") == "ice":
                payload = msg["payload"]
                if payload.get("candidate"):
                    cand = candidate_from_sdp(payload["candidate"])
                    cand.sdpMid = payload.get("sdpMid")
                    cand.sdpMLineIndex = payload.get("sdpMLineIndex")
                    await self.pc.addIceCandidate(cand)
                else:
                    await self.pc.addIceCandidate(None)
            return mtype

        try:
            async for raw in self._ws:
                if raw.type != WSMsgType.TEXT:
                    break
                await handle(json.loads(raw.data))
        except Exception:
            pass

    async def close(self) -> None:
        if self.pc is not None:
            await self.pc.close()
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_json({"type": "leave"})
            await self._ws.close()
        if self._session is not None:
            await self._session.close()


async def wait_until(predicate, timeout: float = 15.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def _y_plane(frame) -> bytes:
    return bytes(frame.planes[0])


async def test_full_session_with_view_and_control(xvfb_display, signaling_server):
    # A real window with known colour: its pixels must arrive at the technician.
    close_win, _win, _d = draw_window(xvfb_display, 400, 300, 300, 200, (220, 40, 60))
    try:
        cb = Recorder(consent=(True, True))
        cfg = CustomerConfig(
            signaling_url=signaling_server.ws_url,
            code="1234",
            display=xvfb_display,
            fps=10,
            ice_servers=[],
            media_timeout=10,
            connect_timeout=25,
        )
        session = CustomerSession(cfg, cb)
        cb.session = session
        session.start_background()

        assert await wait_until(
            lambda: CustomerState.WAITING in [s for s, _ in cb.states]
        ), f"customer never reached WAITING: {cb.states}"

        peer = TechPeer(signaling_server.ws_url)
        tech_task = asyncio.ensure_future(peer.run())

        # 1) technician arrives -> customer consent screen fires
        assert await wait_until(lambda: cb.technician_arrived.is_set()), \
            "customer was never notified about the technician"
        assert CustomerState.CONSENT in [s for s, _ in cb.states]

        # 2) WebRTC comes up and real video frames arrive
        assert await wait_until(lambda: len(peer.frames) > 5, timeout=25), \
            f"no video frames received (states={peer.states}, customer={cb.states})"
        assert await wait_until(
            lambda: CustomerState.ACTIVE in [s for s, _ in cb.states]
        ), f"customer never reached ACTIVE: {cb.states}"

        frame = peer.frames[-1]
        assert frame.width == 1280 and frame.height == 800
        y = _y_plane(frame)
        assert max(y) > 40, "video frames look empty (no desktop pixels)"

        # 3) input channel: move the pointer and verify the real cursor moved
        assert await wait_until(lambda: peer.channel is not None), "no input channel"
        exp_x, exp_y = CustomerSession._map_point(0.75, 0.25, FULL_GEOM)
        peer.channel.send(json.dumps({"type": "move", "x": 0.75, "y": 0.25}))

        def moved():
            try:
                return query_pointer(xvfb_display) == (exp_x, exp_y)
            except Exception:
                return False

        assert await wait_until(moved, timeout=8), \
            f"pointer did not move to {(exp_x, exp_y)}; got {query_pointer(xvfb_display)}"

        # click + key events must not raise anywhere
        peer.channel.send(json.dumps({"type": "button", "button": "left",
                                      "down": True, "x": 0.5, "y": 0.5}))
        peer.channel.send(json.dumps({"type": "button", "button": "left",
                                      "down": False, "x": 0.5, "y": 0.5}))
        peer.channel.send(json.dumps({"type": "key", "key": "Escape", "down": True}))
        peer.channel.send(json.dumps({"type": "key", "key": "Escape", "down": False}))

        # 4) technician ends the session -> remote access stops immediately
        await peer._ws.send_json({"type": "leave"})
        assert await wait_until(lambda: bool(cb.ended), timeout=10), \
            "customer did not notice the session ended"
        assert session._capturer is None, "screen capture still running after end"
        assert session._input is None, "input control still active after end"
        assert CustomerState.ENDED in [s for s, _ in cb.states]

        await peer.close()
        await asyncio.sleep(0.2)
        tech_task.cancel()
    finally:
        close_win()


async def test_control_denied_blocks_input(xvfb_display, signaling_server):
    """View-only consent: video flows, input events are ignored."""
    cb = Recorder(consent=(True, False))
    cfg = CustomerConfig(
        signaling_url=signaling_server.ws_url, code="1234", display=xvfb_display,
        fps=8, ice_servers=[], media_timeout=10, connect_timeout=25,
    )
    session = CustomerSession(cfg, cb)
    cb.session = session
    session.start_background()
    assert await wait_until(
        lambda: CustomerState.WAITING in [s for s, _ in cb.states]
    ), f"customer never reached WAITING: {cb.states}"

    peer = TechPeer(signaling_server.ws_url)
    tech_task = asyncio.ensure_future(peer.run())
    assert await wait_until(lambda: cb.technician_arrived.is_set()), \
        "customer was never notified about the technician"

    # park the pointer somewhere known
    pre = InputController(xvfb_display)
    pre.pointer_move(7, 7)
    pre.wait_idle()
    pre.close()

    assert await wait_until(lambda: len(peer.frames) > 3, timeout=25), \
        "view-only session produced no video"
    assert await wait_until(lambda: peer.channel is not None)

    peer.channel.send(json.dumps({"type": "move", "x": 0.9, "y": 0.9}))
    await asyncio.sleep(1.2)
    assert query_pointer(xvfb_display) == (7, 7), \
        "input was injected even though control was denied"

    session.stop("test done")
    await peer.close()
    tech_task.cancel()


async def test_view_denied_sends_no_video(xvfb_display, signaling_server):
    """Control-only consent is refused: the screen must not be captured."""
    cb = Recorder(consent=(False, True))
    cfg = CustomerConfig(
        signaling_url=signaling_server.ws_url, code="1234", display=xvfb_display,
        fps=8, ice_servers=[], media_timeout=10, connect_timeout=25,
    )
    session = CustomerSession(cfg, cb)
    cb.session = session
    session.start_background()
    assert await wait_until(
        lambda: CustomerState.WAITING in [s for s, _ in cb.states]
    ), f"customer never reached WAITING: {cb.states}"

    peer = TechPeer(signaling_server.ws_url)
    tech_task = asyncio.ensure_future(peer.run())
    assert await wait_until(lambda: cb.technician_arrived.is_set()), \
        "customer was never notified about the technician"

    await asyncio.sleep(4.0)
    assert peer.frames == [], "video was streamed although the customer denied viewing"
    assert session._capturer is None, "capture started despite view denial"

    session.stop("test done")
    await peer.close()
    tech_task.cancel()


async def test_unreachable_signaling_reports_real_error():
    cb = Recorder()
    cfg = CustomerConfig(
        signaling_url="ws://127.0.0.1:9/ws", code="1234", display=":0",
        ice_servers=[], media_timeout=3,
    )
    session = CustomerSession(cfg, cb)
    session.start_background()
    assert await wait_until(lambda: bool(cb.errors) or bool(cb.ended), timeout=12), \
        "no failure reported for an unreachable signaling server"
    assert any("could not reach the support server" in e for e in cb.errors), cb.errors
    assert CustomerState.ERROR in [s for s, _ in cb.states]
