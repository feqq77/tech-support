"""Support signaling server.

Pairs a waiting customer with an available technician by support code and
relays WebRTC negotiation messages (SDP/ICE) and consent state between them.
All media and input traffic flows directly over end-to-end encrypted WebRTC
(DTLS-SRTP) between the customer application and the technician browser; this
server never sees screen content or keystrokes.

Security properties:
  * Technician connections MUST present the technician token (SUPPORT_TECH_TOKEN).
    Comparison is constant-time (secrets.compare_digest).
  * Support codes are room identifiers.  This alpha build uses the fixed
    support code "1234" (change it with --code if you ever need to).
  * Messages are only relayed inside a paired session; unpaired sockets can
    talk to nobody.
  * Binds to 127.0.0.1 by default.  Publish it on a real host behind TLS
    (see docs/DEPLOY.md) - never expose it unencrypted on the public internet.

Run locally for testing:
    .venv/bin/python -m server.signaling_server --port 8787 --tech-token secret
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from aiohttp import WSMsgType, web

from app import protocol
from app import config

log = logging.getLogger("support.signaling-server")

DEFAULT_STUN = ["stun:stun.l.google.com:19302"]


@dataclass
class Peer:
    ws: web.WebSocketResponse
    role: str
    code: str
    session_id: str
    joined_at: float = field(default_factory=time.time)
    peer: Optional["Peer"] = None


class SessionManager:
    def __init__(self, tech_token: str, ice_servers: list, support_code: str,
                 max_rooms: int = 200) -> None:
        self.tech_token = tech_token
        self.ice_servers = ice_servers
        self.support_code = support_code
        self.max_rooms = max_rooms
        self.rooms: dict[str, dict[str, list[Peer]]] = {}
        self._lock = asyncio.Lock()

    async def join(self, peer: Peer) -> Optional[Peer]:
        """Register a peer and try to pair it.  Returns the paired peer (if any)."""
        async with self._lock:
            room = self.rooms.setdefault(peer.code, {"customer": [], "technician": []})
            log.info("join %s session=%s: waiting customers=%d technicians=%d",
                     peer.role, peer.session_id,
                     len(room["customer"]), len(room["technician"]))
            if peer.role == protocol.ROLE_CUSTOMER:
                waiting_techs = room["technician"]
                tech = waiting_techs.pop(0) if waiting_techs else None
                if tech is None:
                    room["customer"].append(peer)
                else:
                    peer.peer = tech
                    tech.peer = peer
                return tech
            waiting_customers = room["customer"]
            cust = waiting_customers.pop(0) if waiting_customers else None
            if cust is None:
                room["technician"].append(peer)
            else:
                peer.peer = cust
                cust.peer = peer
            return cust

    async def leave(self, peer: Peer) -> Optional[Peer]:
        """Unregister a peer; returns the former partner (to be notified)."""
        async with self._lock:
            partner = peer.peer
            if partner is not None:
                partner.peer = None
            peer.peer = None
            room = self.rooms.get(peer.code)
            if room is not None:
                for lst in (room["customer"], room["technician"]):
                    if peer in lst:
                        lst.remove(peer)
                if not room["customer"] and not room["technician"]:
                    self.rooms.pop(peer.code, None)
            return partner

    def stats(self) -> dict:
        return {
            "rooms": len(self.rooms),
            "waiting_customers": sum(len(r["customer"]) for r in self.rooms.values()),
            "waiting_technicians": sum(len(r["technician"]) for r in self.rooms.values()),
            "paired": sum(
                1 for r in self.rooms.values()
                for p in r["customer"] if p.peer is not None
            ),
        }


def _load_ice_servers(args) -> list:
    raw = args.ice_servers or os.environ.get("ICE_SERVERS_JSON", "")
    if raw.strip():
        try:
            servers = json.loads(raw)
            if isinstance(servers, list):
                return servers
        except ValueError as exc:
            raise SystemExit(f"--ice-servers is not valid JSON: {exc}")
    return [{"urls": u} for u in DEFAULT_STUN]


def build_app(tech_token: str, ice_servers: list,
              support_code: str = protocol.DEFAULT_SUPPORT_CODE) -> web.Application:
    if not tech_token:
        raise SystemExit(
            "a technician token is required: pass --tech-token or set "
            "SUPPORT_TECH_TOKEN (generate one with: python3 -c "
            "'import secrets; print(secrets.token_urlsafe(24))')"
        )
    manager = SessionManager(
        tech_token=tech_token,
        ice_servers=ice_servers,
        support_code=support_code,
    )
    app = web.Application()
    app["manager"] = manager

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, **manager.stats()})

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=30.0)
        await ws.prepare(request)
        peer: Optional[Peer] = None
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                try:
                    data = protocol.decode(msg.data)
                except protocol.ProtocolError as exc:
                    await ws.send_json(protocol.error(protocol.ERR_BAD_MESSAGE, str(exc)))
                    continue
                mtype = data.get("type")
                if peer is None:
                    if mtype != protocol.MSG_HELLO:
                        await ws.send_json(
                            protocol.error(protocol.ERR_BAD_MESSAGE, "hello required first")
                        )
                        break
                    try:
                        role, code = protocol.validate_hello(data)
                    except protocol.ProtocolError as exc:
                        await ws.send_json(
                            protocol.error(protocol.ERR_BAD_MESSAGE, str(exc))
                        )
                        break
                    if role == protocol.ROLE_TECHNICIAN:
                        token = str(data.get("token") or "")
                        if not secrets.compare_digest(token, manager.tech_token):
                            await ws.send_json(
                                protocol.error(
                                    protocol.ERR_BAD_TOKEN,
                                    "technician authentication failed: invalid token",
                                )
                            )
                            break
                    if code != manager.support_code:
                        await ws.send_json(
                            protocol.error(
                                protocol.ERR_BAD_CODE,
                                "wrong support code - this server uses code "
                                f"{manager.support_code}",
                            )
                        )
                        break
                    peer = Peer(
                        ws=ws, role=role, code=code,
                        session_id=secrets.token_urlsafe(9),
                    )
                    partner = await manager.join(peer)
                    await _send(peer, {
                        "type": protocol.MSG_WELCOME,
                        "session": peer.session_id,
                        "peer": partner.role if partner else None,
                        "ice_servers": manager.ice_servers,
                    })
                    if partner is not None:
                        await _send(partner, {
                            "type": protocol.MSG_PEER_JOINED,
                            "role": peer.role,
                            "session": peer.session_id,
                        })
                    log.info(
                        "%s joined code=%r session=%s partner=%s",
                        role, code, peer.session_id,
                        partner.role if partner else "none",
                    )
                    continue

                if mtype == protocol.MSG_LEAVE:
                    break
                if mtype == protocol.MSG_PING:
                    await ws.send_json({
                        "type": protocol.MSG_PONG,
                        # Answering with the pairing state lets a client
                        # recover if a one-shot "peer-joined" notification
                        # was ever lost (keepalive pings repeat).
                        "peer": peer.peer.role if peer.peer else None,
                    })
                    continue
                if mtype == protocol.MSG_PONG:
                    continue
                partner = peer.peer
                if partner is None:
                    continue  # nobody to talk to; ignore
                if mtype == protocol.MSG_SIGNAL:
                    try:
                        kind, payload = protocol.validate_signal(data)
                    except protocol.ProtocolError as exc:
                        await ws.send_json(
                            protocol.error(protocol.ERR_BAD_MESSAGE, str(exc))
                        )
                        continue
                    await _send(partner, {
                        "type": protocol.MSG_SIGNAL,
                        "kind": kind,
                        "payload": payload,
                        "from": peer.role,
                    })
                elif mtype == protocol.MSG_CONSENT:
                    await _send(partner, {
                        "type": protocol.MSG_CONSENT,
                        "view": bool(data.get("view")),
                        "control": bool(data.get("control")),
                    })
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("websocket session error")
        finally:
            if peer is not None:
                partner = await manager.leave(peer)
                if partner is not None:
                    await _send(partner, {
                        "type": protocol.MSG_PEER_LEFT,
                        "role": peer.role,
                    })
                log.info("%s left code=%r session=%s", peer.role, peer.code, peer.session_id)
        return ws

    async def _send(peer: Peer, msg: dict) -> None:
        try:
            if peer.ws.closed:
                log.warning("dropping %r for %s session=%s: websocket already closed",
                            msg.get("type"), peer.role, peer.session_id)
                return
            log.info("send %r to %s session=%s", msg.get("type"), peer.role,
                     peer.session_id)
            await peer.ws.send_json(msg)
        except Exception:
            log.warning("failed to send %r to %s session=%s",
                        msg.get("type"), peer.role, peer.session_id,
                        exc_info=True)

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/ws", ws_handler)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Support signaling server")
    parser.add_argument("--host", default=os.environ.get("SIGNALING_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("SIGNALING_PORT", "8787")))
    # Falls back to the project config.env like the apps do
    # (precedence: environment variables > config.env > defaults).
    cfg = config.load_config()
    parser.add_argument("--tech-token", default=cfg.tech_token)
    parser.add_argument("--ice-servers",
                        default=cfg.ice_servers_json,
                        help='JSON list, e.g. [{"urls":"turn:turn.example.com:3478",'
                             '"username":"u","credential":"p"}]')
    parser.add_argument("--code", default=cfg.support_code or
                        protocol.DEFAULT_SUPPORT_CODE,
                        help="the support code customers and technicians type "
                             "(default: 1234)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = build_app(
        tech_token=args.tech_token,
        ice_servers=_load_ice_servers(args),
        support_code=args.code,
    )
    log.info(
        "signaling server listening on http://%s:%d/ws (tech auth: %s)",
        args.host, args.port,
        "token required" if args.tech_token else "NO TOKEN - refusing to start"
        if not args.tech_token else "",
    )
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
