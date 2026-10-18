"""Signaling server protocol tests with real websocket clients."""

from __future__ import annotations

import pytest
from aiohttp import ClientSession, WSMsgType

from tests.conftest import TECH_TOKEN, SignalingServerThread


async def ws_connect(url: str):
    session = ClientSession()
    ws = await session.ws_connect(url, timeout=5)
    return session, ws


async def recv_json(ws):
    msg = await ws.receive(timeout=5)
    assert msg.type == WSMsgType.TEXT, f"unexpected ws message: {msg.type}"
    import json
    return json.loads(msg.data)


async def recv_until(ws, mtype: str, limit: int = 10):
    for _ in range(limit):
        msg = await recv_json(ws)
        if msg["type"] == mtype:
            return msg
    raise AssertionError(f"never received a {mtype!r} message")


async def test_technician_token_required(signaling_server):
    session, ws = await ws_connect(signaling_server.ws_url)
    try:
        await ws.send_json({"type": "hello", "role": "technician", "code": "1234",
                            "token": "wrong"})
        msg = await recv_json(ws)
        assert msg["type"] == "error"
        assert msg["code"] == "bad-token"
    finally:
        await session.close()


async def test_bad_hello_rejected(signaling_server):
    session, ws = await ws_connect(signaling_server.ws_url)
    try:
        await ws.send_json({"type": "hello", "role": "wizard", "code": "1234"})
        msg = await recv_json(ws)
        assert msg["type"] == "error" and msg["code"] == "bad-message"
    finally:
        await session.close()


async def test_pairing_and_signal_relay(signaling_server):
    cust_s, cust = await ws_connect(signaling_server.ws_url)
    tech_s, tech = await ws_connect(signaling_server.ws_url)
    try:
        await cust.send_json({"type": "hello", "role": "customer", "code": "1234"})
        msg = await recv_json(cust)
        assert msg["type"] == "welcome"
        assert msg["peer"] is None
        assert isinstance(msg["ice_servers"], list)

        await tech.send_json({"type": "hello", "role": "technician", "code": "1234",
                              "token": TECH_TOKEN})
        msg = await recv_json(tech)
        assert msg["type"] == "welcome" and msg["peer"] == "customer"

        joined = await recv_until(cust, "peer-joined")
        assert joined["role"] == "technician"

        # relay offer customer -> technician
        await cust.send_json({"type": "signal", "kind": "offer",
                              "payload": {"type": "offer", "sdp": "v=0 fake"}})
        got = await recv_until(tech, "signal")
        assert got["kind"] == "offer" and got["payload"]["sdp"] == "v=0 fake"
        assert got["from"] == "customer"

        # relay answer technician -> customer
        await tech.send_json({"type": "signal", "kind": "answer",
                              "payload": {"type": "answer", "sdp": "v=0 ans"}})
        got = await recv_until(cust, "signal")
        assert got["kind"] == "answer"

        # relay trickle ICE
        await tech.send_json({"type": "signal", "kind": "ice",
                              "payload": {"candidate": "candidate:1 1 udp 1 127.0.0.1 1 typ host",
                                          "sdpMid": "0", "sdpMLineIndex": 0}})
        got = await recv_until(cust, "signal")
        assert got["kind"] == "ice" and got["payload"]["sdpMid"] == "0"

        # consent relay customer -> technician
        await cust.send_json({"type": "consent", "view": True, "control": False})
        got = await recv_until(tech, "consent")
        assert got == {"type": "consent", "view": True, "control": False}

        # disconnect notification
        await cust.send_json({"type": "leave"})
        got = await recv_until(tech, "peer-left")
        assert got["role"] == "customer"
    finally:
        await cust_s.close()
        await tech_s.close()


async def test_ping_pong_reports_pairing_state(signaling_server):
    """Keepalive pong carries the pairing state.

    "peer-joined" is a one-shot notification; if it is ever lost, the client's
    repeating keepalive ping must be able to recover the pairing state."""
    cust_s, cust = await ws_connect(signaling_server.ws_url)
    try:
        await cust.send_json({"type": "hello", "role": "customer", "code": "1234"})
        msg = await recv_json(cust)
        assert msg["type"] == "welcome"

        await cust.send_json({"type": "ping"})
        msg = await recv_until(cust, "pong")
        assert msg["peer"] is None

        tech_s, tech = await ws_connect(signaling_server.ws_url)
        try:
            await tech.send_json({"type": "hello", "role": "technician", "code": "1234",
                                  "token": TECH_TOKEN})
            await recv_until(tech, "welcome")

            await cust.send_json({"type": "ping"})
            msg = await recv_until(cust, "pong")
            assert msg["peer"] == "technician"
        finally:
            await tech_s.close()
    finally:
        await cust_s.close()
async def test_no_cross_session_leak(signaling_server):
    """Two concurrent sessions on the shared code must not see each other's traffic."""
    a_s, a = await ws_connect(signaling_server.ws_url)
    b_s, b = await ws_connect(signaling_server.ws_url)
    t_s, tech = await ws_connect(signaling_server.ws_url)
    try:
        await a.send_json({"type": "hello", "role": "customer", "code": "1234"})
        await recv_json(a)
        await b.send_json({"type": "hello", "role": "customer", "code": "1234"})
        await recv_json(b)
        # the technician pairs with the first waiting customer (a)
        await tech.send_json({"type": "hello", "role": "technician", "code": "1234",
                              "token": TECH_TOKEN})
        msg = await recv_json(tech)
        assert msg["type"] == "welcome" and msg["peer"] == "customer"

        await a.send_json({"type": "signal", "kind": "offer",
                           "payload": {"type": "offer", "sdp": "secret"}})
        got = await recv_until(tech, "signal")
        assert got["payload"]["sdp"] == "secret"

        import asyncio
        with pytest.raises(asyncio.TimeoutError):
            await recv_json(b)  # the other waiting customer must never see it
    finally:
        await a_s.close()
        await b_s.close()
        await t_s.close()


async def test_support_code_is_1234(signaling_server):
    """The alpha support code 1234 works like any normal code."""
    session, ws = await ws_connect(signaling_server.ws_url)
    try:
        await ws.send_json({"type": "hello", "role": "customer", "code": "1234"})
        msg = await recv_json(ws)
        assert msg["type"] == "welcome" and msg["peer"] is None
    finally:
        await session.close()


async def test_wrong_code_rejected(signaling_server):
    session, ws = await ws_connect(signaling_server.ws_url)
    try:
        await ws.send_json({"type": "hello", "role": "customer", "code": "9999"})
        msg = await recv_json(ws)
        assert msg["type"] == "error" and msg["code"] == "bad-code"
        assert "1234" in msg["message"]
    finally:
        await session.close()


async def test_custom_code_option():
    """--code lets a deployment pick a different single code."""
    srv = SignalingServerThread(support_code="5678").start()
    try:
        session, ws = await ws_connect(srv.ws_url)
        await ws.send_json({"type": "hello", "role": "customer", "code": "1234"})
        msg = await recv_json(ws)
        assert msg["type"] == "error" and msg["code"] == "bad-code"
        await session.close()

        session, ws = await ws_connect(srv.ws_url)
        await ws.send_json({"type": "hello", "role": "customer", "code": "5678"})
        msg = await recv_json(ws)
        assert msg["type"] == "welcome"
        await session.close()
    finally:
        srv.stop()
