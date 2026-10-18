"""Technician console server tests: HTTP endpoints and the WS signaling proxy."""

from __future__ import annotations

import asyncio
import json
import socket
import threading

import pytest
from aiohttp import ClientSession, WSMsgType, web

from technician.technician_server import TechnicianServer
from tests.conftest import TECH_TOKEN


class TechnicianServerThread:
    def __init__(self, signaling_url: str, tech_token: str = TECH_TOKEN):
        self.signaling_url = signaling_url
        self.tech_token = tech_token
        self.port: int | None = None
        self._ready = threading.Event()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def start(self) -> "TechnicianServerThread":
        def runner():
            async def main():
                srv = TechnicianServer(
                    signaling_url=self.signaling_url,
                    tech_token=self.tech_token,
                )
                app = srv.build_app()
                runner = web.AppRunner(app)
                await runner.setup()
                sock = socket.socket()
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", 0))
                self.port = sock.getsockname()[1]
                await web.SockSite(runner, sock).start()
                self._ready.set()
                while True:
                    await asyncio.sleep(3600)

            asyncio.run(main())

        threading.Thread(target=runner, daemon=True).start()
        assert self._ready.wait(timeout=8), "technician server failed to start"
        return self


@pytest.fixture()
async def fake_signaling():
    """A fake signaling server that records what it receives."""
    received: list[dict] = []
    ready = asyncio.Event()

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                received.append(data)
                if data.get("type") == "hello":
                    await ws.send_json({
                        "type": "welcome", "session": "s1", "peer": None,
                        "ice_servers": [],
                    })
        return ws

    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    try:
        yield f"ws://127.0.0.1:{port}/ws", received
    finally:
        await runner.cleanup()


async def test_serves_console_ui():
    srv = TechnicianServerThread("ws://127.0.0.1:1/ws").start()
    async with ClientSession() as session:
        async with session.get(f"{srv.base_url}/") as resp:
            assert resp.status == 200
            body = await resp.text()
            assert "Technician Console" in body
            assert "tech check" not in body  # customer UI never served here
        async with session.get(f"{srv.base_url}/static/app.js") as resp:
            assert resp.status == 200
            assert "RTCPeerConnection" in await resp.text()
        async with session.get(f"{srv.base_url}/api/config") as resp:
            cfg = await resp.json()
            assert cfg["tech_token_configured"] is True
        async with session.get(f"{srv.base_url}/healthz") as resp:
            assert (await resp.json())["ok"] is True


async def test_ws_proxy_injects_technician_token(fake_signaling):
    fake_url, received = fake_signaling
    srv = TechnicianServerThread(fake_url).start()
    async with ClientSession() as session:
        ws = await session.ws_connect(srv.ws_url)
        await ws.send_json({"type": "hello", "role": "technician", "code": "1234"})
        msg = await ws.receive(timeout=5)
        data = json.loads(msg.data)
        assert data["type"] == "welcome"
        await ws.close()
    assert received and received[0]["type"] == "hello"
    # The browser never sends a token; the console server must inject it.
    assert received[0]["token"] == TECH_TOKEN
    assert received[0]["role"] == "technician"


async def test_ws_proxy_reports_unreachable_signaling():
    srv = TechnicianServerThread("ws://127.0.0.1:9/ws").start()
    async with ClientSession() as session:
        ws = await session.ws_connect(srv.ws_url)
        await ws.send_json({"type": "hello", "role": "technician", "code": "1234"})
        msg = await ws.receive(timeout=10)
        data = json.loads(msg.data)
        assert data["type"] == "error"
        assert data["code"] == "signaling-unreachable"
        assert "127.0.0.1:9" in data["message"]  # the real failure is named
        await ws.close()


async def test_ws_proxy_relays_signals_both_ways(fake_signaling):
    fake_url, received = fake_signaling
    srv = TechnicianServerThread(fake_url).start()
    async with ClientSession() as session:
        ws = await session.ws_connect(srv.ws_url)
        await ws.send_json({"type": "hello", "role": "technician", "code": "7777"})
        await ws.receive(timeout=5)  # welcome
        await ws.send_json({"type": "signal", "kind": "ice",
                            "payload": {"candidate": "candidate:1"}})
        await ws.send_json({"type": "leave"})
        await asyncio.sleep(0.3)
        await ws.close()
    kinds = [m.get("kind") for m in received if m.get("type") == "signal"]
    assert "ice" in kinds


def test_refuses_non_loopback_bind():
    with pytest.raises(SystemExit):
        TechnicianServer("ws://x/ws", tech_token="t", host="0.0.0.0")


def test_refuses_missing_token():
    with pytest.raises(SystemExit):
        TechnicianServer("ws://x/ws", tech_token="")
