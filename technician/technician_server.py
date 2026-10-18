"""Technician console server.

Serves the technician web UI and proxies its signaling traffic to the support
signaling server.  Binds to 127.0.0.1 ONLY - this is an operator console, not
a remotely accessible control panel.  The technician's browser connects to the
customer directly over end-to-end encrypted WebRTC.

Run:
    .venv/bin/python -m technician.technician_server
Then open http://127.0.0.1:8765 in Chrome.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import time
from collections import deque
from typing import Optional

import aiohttp
from aiohttp import WSMsgType, web

log = logging.getLogger("support.technician-server")

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
LOOPBACK = ("127.0.0.1", "::1", "localhost")

# Reconnect-storm guard.  A runaway client (e.g. a stale page stuck in a
# reconnect loop) would otherwise register a fresh technician session at the
# matchmaker every few seconds and steal pairings with connections that die
# immediately.  Legitimate use never approaches this rate.
WS_RATE_WINDOW = 10.0   # seconds
WS_RATE_MAX = 10        # websockets per window before the guard trips


class TechnicianServer:
    def __init__(self, signaling_url: str, tech_token: str,
                 host: str = "127.0.0.1", port: int = 8765) -> None:
        if host not in LOOPBACK:
            raise SystemExit(
                f"refusing to bind the technician console to {host!r}: it must stay "
                "on 127.0.0.1. The console is an unauthenticated local operator "
                "panel; use the signaling server for cross-computer access."
            )
        if not tech_token:
            raise SystemExit(
                "no technician token configured. Set SUPPORT_TECH_TOKEN or pass "
                "--tech-token (it must match the signaling server's token)."
            )
        self.signaling_url = signaling_url
        self.tech_token = tech_token
        self.host = host
        self.port = port

    def build_app(self) -> web.Application:
        app = web.Application()
        app["server"] = self

        async def index(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(STATIC_DIR / "index.html")

        async def config(_request: web.Request) -> web.Response:
            return web.json_response({
                "signaling_url": self.signaling_url,
                "tech_token_configured": bool(self.tech_token),
            })

        async def healthz(_request: web.Request) -> web.Response:
            return web.json_response({"ok": True, "bound_to": self.host})

        recent_ws: deque[float] = deque()

        async def ws_proxy(request: web.Request) -> web.WebSocketResponse:
            client_ip = request.remote
            if client_ip not in LOOPBACK:
                return web.Response(status=403, text="loopback clients only")
            now = time.monotonic()
            while recent_ws and now - recent_ws[0] > WS_RATE_WINDOW:
                recent_ws.popleft()
            if len(recent_ws) >= WS_RATE_MAX:
                log.warning("refusing technician websocket: reconnect storm guard")
                return web.Response(status=429, text="too many reconnects — slow down")
            recent_ws.append(now)  # accepted connections only: a runaway
            # client must never starve a real one out of the window.
            ws = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=30.0)
            await ws.prepare(request)
            log.info("technician UI connected from %s", client_ip)

            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10.0)
            )
            upstream: Optional[aiohttp.ClientWebSocketResponse] = None

            async def connect_upstream() -> aiohttp.ClientWebSocketResponse:
                try:
                    return await session.ws_connect(
                        self.signaling_url, timeout=10.0,
                        headers={"User-Agent": "support-technician/1"},
                    )
                except Exception as exc:
                    raise web.HTTPBadGateway(
                        text=f"cannot reach signaling server {self.signaling_url}: {exc}"
                    )

            async def pump_upstream() -> None:
                try:
                    async for m in upstream:
                        if m.type == WSMsgType.TEXT:
                            if not ws.closed:
                                await ws.send_str(m.data)
                        elif m.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                            break
                except Exception as exc:
                    log.warning("upstream pump error: %s", exc)
                finally:
                    if not ws.closed:
                        await ws.send_json({
                            "type": "error",
                            "code": "signaling-lost",
                            "message": "connection to the support signaling server was lost",
                        })
                        await ws.close()

            upstream_task: Optional[asyncio.Task] = None
            try:
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        break
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    if upstream is None or upstream.closed:
                        if upstream_task is not None:
                            upstream_task.cancel()
                        upstream = await connect_upstream()
                        upstream_task = asyncio.create_task(pump_upstream())
                    # Authenticate the technician; never trust a browser token.
                    if data.get("type") == "hello":
                        data["token"] = self.tech_token
                        data.setdefault("role", "technician")
                    await upstream.send_str(json.dumps(data))
            except web.HTTPBadGateway as exc:
                log.warning("upstream connect failed: %s", exc.text)
                if not ws.closed:
                    await ws.send_json({
                        "type": "error",
                        "code": "signaling-unreachable",
                        "message": exc.text,
                    })
                    await ws.close()
            except Exception:
                log.exception("technician ws proxy error")
            finally:
                if upstream is not None and not upstream.closed:
                    try:
                        await upstream.send_str(json.dumps({"type": "leave"}))
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(upstream.close(), 5.0)  # must run even if the leave failed
                    except Exception:
                        pass
                if upstream_task is not None:
                    upstream_task.cancel()
                await session.close()
                log.info("technician UI disconnected")
            return ws

        app.router.add_get("/", index)
        app.router.add_get("/api/config", config)
        app.router.add_get("/healthz", healthz)
        app.router.add_get("/ws", ws_proxy)
        app.router.add_static("/static/", STATIC_DIR, show_index=False)
        return app

    def run(self) -> None:
        app = self.build_app()
        log.info("technician console: http://%s:%d (signaling: %s)",
                 self.host, self.port, self.signaling_url)
        web.run_app(app, host=self.host, port=self.port, print=None)


def main() -> None:
    from app.config import load_config

    cfg = load_config()
    parser = argparse.ArgumentParser(description="Technician console server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("TECH_PORT", "8765")))
    parser.add_argument("--signaling-url",
                        default=os.environ.get("SIGNALING_URL", cfg.signaling_url))
    parser.add_argument("--tech-token",
                        default=os.environ.get("SUPPORT_TECH_TOKEN", cfg.tech_token))
    parser.add_argument("--open", action="store_true",
                        help="open the console in a browser")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = TechnicianServer(
        signaling_url=args.signaling_url,
        tech_token=args.tech_token,
        host=args.host,
        port=args.port,
    )
    if args.open:
        import webbrowser
        webbrowser.open(f"http://{args.host}:{args.port}")
    server.run()


if __name__ == "__main__":
    main()
