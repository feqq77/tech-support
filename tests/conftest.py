"""Shared test fixtures: a real Xvfb display and a real signaling server."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import threading
import time

import pytest

XVFB_NUM = 106
XVFB_DISPLAY = f"127.0.0.1:{XVFB_NUM}"
TECH_TOKEN = "test-technician-token"


def _wait_for_tcp(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


@pytest.fixture(scope="session")
def xvfb_display():
    """A real Xvfb X server at 127.0.0.1:106 (1280x800), reachable over TCP."""
    proc = subprocess.Popen(
        ["Xvfb", f":{XVFB_NUM}", "-screen", "0", "1280x800x24", "-listen", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for_tcp(6000 + XVFB_NUM), "Xvfb did not start listening on TCP"
        # Make sure access control allows local connections without xauth.
        subprocess.run(
            ["xdpyinfo", "-display", XVFB_DISPLAY], check=True,
            capture_output=True, timeout=5,
        )
        yield XVFB_DISPLAY
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def draw_window(display: str, x: int, y: int, w: int, h: int, rgb: tuple[int, int, int]):
    """Map a solid-colour window on a display and return a close() callable."""
    from Xlib import X, display as xdisplay

    d = xdisplay.Display(display)
    screen = d.screen()
    pixel = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
    win = screen.root.create_window(
        x, y, w, h, 0, screen.root_depth,
        X.InputOutput, X.CopyFromParent,
        background_pixel=pixel,
        event_mask=X.ExposureMask | X.KeyPressMask | X.KeyReleaseMask,
    )
    win.map()
    d.sync()
    time.sleep(0.15)

    def close():
        try:
            win.destroy()
            d.sync()
            d.close()
        except Exception:
            pass

    return close, win, d


class SignalingServerThread:
    """Runs the signaling server on a free localhost port in a thread."""

    def __init__(self, tech_token: str = TECH_TOKEN, support_code: str = "1234",
                 ice_servers: list | None = None):
        self.tech_token = tech_token
        self.support_code = support_code
        self.ice_servers = ice_servers or [{"urls": "stun:stun.l.google.com:19302"}]
        self.port: int | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner = None

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def start(self) -> "SignalingServerThread":
        ready = threading.Event()

        def runner():
            from aiohttp import web
            from server.signaling_server import build_app

            async def main():
                self._loop = asyncio.get_running_loop()
                app = build_app(
                    tech_token=self.tech_token,
                    ice_servers=self.ice_servers,
                    support_code=self.support_code,
                )
                runner = web.AppRunner(app)
                await runner.setup()
                # pick a free port
                sock = socket.socket()
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", 0))
                self.port = sock.getsockname()[1]
                site = web.SockSite(runner, sock)
                await site.start()
                ready.set()
                while True:
                    await asyncio.sleep(3600)

            asyncio.run(main())

        self._thread = threading.Thread(target=runner, name="signaling-server", daemon=True)
        self._thread.start()
        assert ready.wait(timeout=8), "signaling server failed to start"
        return self

    def stop(self) -> None:
        if self._thread is not None:
            # daemon thread + process exit cleans up; nothing long-running left
            self._thread = None


@pytest.fixture(scope="session")
def signaling_server():
    srv = SignalingServerThread().start()
    yield srv
    srv.stop()
