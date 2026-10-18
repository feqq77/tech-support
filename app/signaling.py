"""WebSocket signaling client (used by the customer application).

Speaks the JSON protocol in app/protocol.py against the signaling server
(server/signaling_server.py).  Provides connection timeouts, keepalive and
bounded reconnection with backoff while waiting for a technician.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from . import protocol

log = logging.getLogger("support.signaling")


class SignalingError(RuntimeError):
    pass


class SignalingClient:
    def __init__(
        self,
        url: str,
        role: str,
        code: str,
        token: Optional[str] = None,
        on_message: Optional[Callable[[dict], Awaitable[None]]] = None,
        on_state: Optional[Callable[[str], Awaitable[None]]] = None,
        connect_timeout: float = 10.0,
        welcome_timeout: float = 20.0,
    ) -> None:
        self.url = url
        self.role = role
        self.code = code
        self.token = token
        self.on_message = on_message
        self.on_state = on_state
        self.connect_timeout = connect_timeout
        self.welcome_timeout = welcome_timeout

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        self.auto_reconnect = False  # caller enables while waiting for a peer
        self.connected = False

    # ------------------------------------------------------------------ public

    async def send(self, msg: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise SignalingError("not connected to the signaling server")
        await ws.send_str(protocol.encode(msg))

    async def send_signal(self, kind: str, payload: dict[str, Any]) -> None:
        await self.send(protocol.signal(kind, payload))

    async def send_consent(self, view: bool, control: bool) -> None:
        await self.send({"type": protocol.MSG_CONSENT, "view": view, "control": control})

    async def leave(self) -> None:
        try:
            await self.send({"type": protocol.MSG_LEAVE})
        except Exception:
            pass

    async def close(self) -> None:
        self._running = False
        self.auto_reconnect = False
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def run(self) -> None:
        """Connect (with retries while auto_reconnect) and pump messages."""
        self._running = True
        attempt = 0
        while self._running:
            try:
                await self._connect_once()
                attempt = 0
                await self._pump()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                log.warning("signaling connection error: %s", exc)
                if self.on_state:
                    await self.on_state(f"signaling error: {exc}")
            if not self._running:
                break
            if not self.auto_reconnect:
                break
            attempt += 1
            delay = min(2.0 * attempt, 15.0)
            log.info("reconnecting to signaling in %.1fs (attempt %d)", delay, attempt)
            if self.on_state:
                await self.on_state(f"reconnecting in {delay:.0f}s")
            await asyncio.sleep(delay)
        await self.close()

    # ----------------------------------------------------------------- internal

    async def _connect_once(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=self.connect_timeout)
            )
        if self.on_state:
            await self.on_state("connecting")
        self._ws = await self._session.ws_connect(self.url, timeout=self.connect_timeout)
        await self.send(protocol.hello(self.role, self.code, self.token))
        # Wait for the server's welcome before considering the link up.
        welcome = await self._ws.receive(timeout=self.welcome_timeout)
        if welcome.type != aiohttp.WSMsgType.TEXT:
            raise SignalingError("server closed the connection before welcome")
        msg = protocol.decode(welcome.data)
        if msg["type"] == protocol.MSG_ERROR:
            raise SignalingError(msg.get("message", "server rejected the connection"))
        if msg["type"] != protocol.MSG_WELCOME:
            raise SignalingError(f"unexpected first message: {msg['type']!r}")
        self.connected = True
        if self.on_state:
            await self.on_state("connected")
        if self.on_message:
            await self.on_message(msg)

    async def _pump(self) -> None:
        assert self._ws is not None
        ws = self._ws
        last_ping = time.monotonic()
        try:
            while self._running and not ws.closed:
                try:
                    item = await ws.receive(timeout=5.0)
                except asyncio.TimeoutError:
                    item = None
                if item is None:
                    if time.monotonic() - last_ping > 20.0:
                        await self.send({"type": protocol.MSG_PING})
                        last_ping = time.monotonic()
                    continue
                if item.type == aiohttp.WSMsgType.TEXT:
                    msg = protocol.decode(item.data)
                    if msg["type"] == protocol.MSG_PING:
                        await self.send({"type": protocol.MSG_PONG})
                        continue
                    if msg["type"] in (protocol.MSG_PONG,):
                        continue
                    if self.on_message:
                        await self.on_message(msg)
                elif item.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
                elif item.type == aiohttp.WSMsgType.ERROR:
                    raise SignalingError(f"websocket error: {ws.exception()}")
        finally:
            self.connected = False
            if self.on_state:
                await self.on_state("disconnected")
