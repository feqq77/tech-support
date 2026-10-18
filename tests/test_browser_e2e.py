"""Browser end-to-end: real Google Chrome drives the technician console.

Verifies the complete two-computer architecture with every real component:
Chrome <-> technician console server (127.0.0.1) <-> signaling server <->
customer session <-> actual X11 display (Xvfb 127.0.0.1:106).

Verified here:
  * the console loads in Chrome and shows connection state
  * the customer's real desktop pixels arrive and render in <video>
    (checked by reading the rendered frame back through a canvas)
  * trusted Chrome mouse events reach the customer's X display at the
    correct mapped coordinates
  * trusted Chrome keyboard events reach X clients on the customer display
  * "End session" immediately stops remote access
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from app.customer import CustomerConfig, CustomerSession, CustomerState
from app.inputctl import query_pointer
from tests.conftest import TECH_TOKEN, draw_window
from tests.cdp import Chrome, find_chrome
from tests.test_e2e import Recorder, wait_until
from tests.test_input import EventPump
from tests.test_technician_server import TechnicianServerThread

pytestmark = pytest.mark.skipif(
    find_chrome() is None, reason="no Chrome/Chromium installed"
)

ARTIFACTS = os.path.join(os.path.dirname(__file__), "artifacts")

RED = (220, 40, 60)


async def _start_customer(xvfb_display, signaling_server, code="1234"):
    cb = Recorder(consent=(True, True))
    cfg = CustomerConfig(
        signaling_url=signaling_server.ws_url, code=code, display=xvfb_display,
        fps=10, ice_servers=[], media_timeout=15, connect_timeout=30,
    )
    session = CustomerSession(cfg, cb)
    cb.session = session
    session.start_background()
    assert await wait_until(
        lambda: CustomerState.WAITING in [s for s, _ in cb.states]
    ), f"customer never waited: {cb.states}"
    return session, cb


async def _page_ready(chrome: Chrome) -> bool:
    return await chrome.evaluate(
        "document.readyState === 'complete' && "
        "!!document.getElementById('connect-btn')"
    )


async def test_full_session_in_real_chrome(xvfb_display, signaling_server, tmp_path):
    os.makedirs(ARTIFACTS, exist_ok=True)

    close_win, _win, _d = draw_window(
        xvfb_display, 400, 300, 300, 200, RED
    )
    # An X client that must receive the keystrokes typed in Chrome.
    pump = EventPump(xvfb_display, 800, 500, 300, 200)

    session, cb = await _start_customer(xvfb_display, signaling_server)
    tech_srv = TechnicianServerThread(signaling_server.ws_url).start()

    chrome = Chrome(headless=True)
    await chrome.launch()
    try:
        # 1) open the technician console in Chrome
        await chrome.navigate(tech_srv.base_url)
        assert await _page_ready(chrome)
        title = await chrome.evaluate("document.title")
        assert title == "Technician Console"
        assert await chrome.evaluate(
            "document.getElementById('status').textContent"
        ) == "disconnected"
        await chrome.screenshot(os.path.join(ARTIFACTS, "console-initial.png"))

        # 2) enter the support code and connect (real clicks)
        await chrome.evaluate(
            "(() => { const i = document.getElementById('code-input');"
            " i.value = '1234'; })()"
        )
        await chrome.click("#connect-btn")

        # 3) customer sees the technician and auto-grants consent in this test
        assert await wait_until(lambda: cb.technician_arrived.is_set(), timeout=20), \
            "customer never saw the technician arrive"

        # 4) video must actually render the customer's desktop
        async def video_live() -> bool:
            return await chrome.evaluate(
                "(() => { const v = document.getElementById('remote-video');"
                " return v.readyState >= 2 && v.videoWidth === 1280; })()"
            )

        deadline = time.time() + 25
        live = False
        while time.time() < deadline:
            if await video_live():
                live = True
                break
            await asyncio.sleep(0.4)
        assert live, "remote video never started in Chrome"

        # read the rendered frame back through a canvas: the red test window
        # must be visible at its expected position on the remote desktop
        pixels = await chrome.evaluate("""
            (() => {
              const v = document.getElementById('remote-video');
              const c = document.createElement('canvas');
              c.width = 1280; c.height = 800;
              const ctx = c.getContext('2d');
              ctx.drawImage(v, 0, 0, 1280, 800);
              const p = ctx.getImageData(550, 400, 1, 1).data;
              return [p[0], p[1], p[2]];
            })()
        """)
        r, g, b = pixels
        assert r > 120 and r > g + 40 and r > b + 20, \
            f"rendered video does not show the customer's red window (pixel {pixels})"
        await chrome.screenshot(os.path.join(ARTIFACTS, "console-live.png"))

        # 5) trusted Chrome mouse input -> real X pointer movement
        coords = await chrome.evaluate("""
            (() => {
              const video = document.getElementById('remote-video');
              const wrap = document.getElementById('screen-wrap');
              const vw = video.videoWidth, vh = video.videoHeight;
              const rect = wrap.getBoundingClientRect();
              const scale = Math.min(rect.width / vw, rect.height / vh);
              const dispW = vw * scale, dispH = vh * scale;
              const offX = (rect.width - dispW) / 2, offY = (rect.height - dispH) / 2;
              return {x: rect.left + offX + 0.75 * dispW,
                      y: rect.top + offY + 0.25 * dispH};
            })()
        """)
        await chrome.mouse("mouseMoved", coords["x"], coords["y"])

        exp = (round(0.75 * 1279), round(0.25 * 799))

        def pointer_ok():
            px, py = query_pointer(xvfb_display)
            return abs(px - exp[0]) <= 2 and abs(py - exp[1]) <= 2

        assert await wait_until(pointer_ok, timeout=8), \
            f"Chrome mouse did not map to {exp}; got {query_pointer(xvfb_display)}"

        # 6) trusted Chrome keyboard -> X client on the customer display.
        # Click a spot that maps onto the EventPump X window (800..1100, 500..700)
        # so XTEST keystrokes land in that client (PointerRoot focus).
        target = await chrome.evaluate("""
            (() => {
              const video = document.getElementById('remote-video');
              const wrap = document.getElementById('screen-wrap');
              const vw = video.videoWidth, vh = video.videoHeight;
              const rect = wrap.getBoundingClientRect();
              const scale = Math.min(rect.width / vw, rect.height / vh);
              const dispW = vw * scale, dispH = vh * scale;
              const offX = (rect.width - dispW) / 2, offY = (rect.height - dispH) / 2;
              return {x: rect.left + offX + (950 / 1279) * dispW,
                      y: rect.top + offY + (600 / 799) * dispH};
            })()
        """)
        await chrome.mouse("mousePressed", target["x"], target["y"])
        await chrome.mouse("mouseReleased", target["x"], target["y"])
        await chrome.key("keyDown", "a", "KeyA", text="a")
        await chrome.key("keyUp", "a", "KeyA")
        from Xlib import X

        ev = await asyncio.to_thread(
            pump.wait_for, lambda e: e.type == X.KeyPress, 6.0
        )
        assert ev is not None, "keystrokes typed in Chrome never reached the X display"

        # 7) End session -> remote access stops immediately
        await chrome.click("#end-btn")
        if not await wait_until(lambda: bool(cb.ended), timeout=10):
            status_now = await chrome.evaluate(
                "document.getElementById('status').textContent"
            )
            page_log = await chrome.evaluate(
                "document.getElementById('log').textContent.slice(0, 300)"
            )
            pytest.fail(
                "customer session did not end when technician pressed End session "
                f"(page status={status_now!r}, log={page_log!r}, "
                f"customer states={cb.states})"
            )
        assert session._capturer is None and session._input is None
        status = await chrome.evaluate(
            "document.getElementById('status').textContent"
        )
        assert status == "session ended"
        await chrome.screenshot(os.path.join(ARTIFACTS, "console-ended.png"))
    finally:
        await chrome.close()
        pump.close()
        close_win()


async def test_second_connect_does_not_loop(signaling_server):
    """Clicking connect again must not start a reconnect storm.

    Regression: connectSignaling() used to close the live socket, and the close
    event scheduled another reconnect 3 s later — which closed the *new*
    socket, and so on forever.  The technician session then reset every 3 s and
    could never pair with a customer.
    """
    tech_srv = TechnicianServerThread(signaling_server.ws_url).start()
    chrome = Chrome(headless=True)
    await chrome.launch()
    try:
        await chrome.navigate(tech_srv.base_url)
        assert await _page_ready(chrome)

        # count every WebSocket the page creates from here on
        await chrome.evaluate(
            "(() => { window.__wsCreated = 0;"
            " const Orig = window.WebSocket;"
            " const W = function (...a) { window.__wsCreated++; return new Orig(...a); };"
            " W.prototype = Orig.prototype;"
            " ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach("
            "   (k) => { W[k] = Orig[k]; });"
            " window.WebSocket = W; })()"
        )
        await chrome.evaluate(
            "(() => { const i = document.getElementById('code-input');"
            " i.value = '1234'; })()"
        )
        await chrome.click("#connect-btn")

        async def status_now():
            return await chrome.evaluate(
                "document.getElementById('status').textContent"
            )

        deadline = time.time() + 15
        while time.time() < deadline:
            if await status_now() == "waiting for customer…":
                break
            await asyncio.sleep(0.3)
        else:
            pytest.fail(f"console never waited for a customer (status={await status_now()!r})")

        # a second click reconnects exactly once (the technician clicks twice)
        await chrome.click("#connect-btn")
        await asyncio.sleep(8)  # the old loop tore the link down every 3 s

        created = await chrome.evaluate("window.__wsCreated")
        assert created <= 2, f"console opened {created} websockets in 8 s — reconnect storm"
        assert await status_now() == "waiting for customer…", (
            f"console did not settle after the second connect (status={await status_now()!r})"
        )
    finally:
        await chrome.close()
