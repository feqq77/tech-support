"""Minimal Chrome DevTools Protocol client (no external dependencies).

Drives a real Google Chrome / Chromium instance: page navigation, JavaScript
evaluation, trusted input events and screenshots.  Used by the browser
end-to-end tests to verify the technician console in an actual browser.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any, Optional

import aiohttp


def find_chrome() -> Optional[str]:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Chrome:
    def __init__(self, headless: bool = True, window_size: tuple[int, int] = (1400, 1000)):
        self.headless = headless
        self.window_size = window_size
        self.proc: Optional[subprocess.Popen] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: list[dict] = []
        self._reader_task: Optional[asyncio.Task] = None
        self.user_data_dir = tempfile.mkdtemp(prefix="chrome-test-")

    async def launch(self) -> None:
        binary = find_chrome()
        if not binary:
            raise RuntimeError("no Chrome/Chromium binary found")
        port = _free_port()
        args = [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--window-size=%d,%d" % self.window_size,
            "about:blank",
        ]
        if self.headless:
            args.insert(1, "--headless=new")
            args.insert(2, "--disable-gpu")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Wait for the DevTools endpoint.
        ws_url = None
        deadline = time.time() + 15
        async with aiohttp.ClientSession() as session:
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError("Chrome exited during startup")
                try:
                    async with session.get(
                        f"http://127.0.0.1:{port}/json/list", timeout=1
                    ) as resp:
                        targets = await resp.json()
                        for target in targets:
                            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                                ws_url = target["webSocketDebuggerUrl"]
                                break
                except Exception:
                    pass
                if ws_url:
                    break
                await asyncio.sleep(0.2)
        if not ws_url:
            raise RuntimeError("could not connect to Chrome DevTools endpoint")

        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(ws_url, timeout=10)
        self._reader_task = asyncio.ensure_future(self._reader())
        await self.send("Page.enable")
        await self.send("Runtime.enable")

    async def _reader(self) -> None:
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            if "id" in data:
                fut = self._pending.pop(data["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(data)
            else:
                self._events.append(data)

    async def send(self, method: str, params: Optional[dict] = None) -> dict:
        self._msg_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._msg_id] = fut
        await self._ws.send_str(json.dumps(
            {"id": self._msg_id, "method": method, "params": params or {}}
        ))
        result = await asyncio.wait_for(fut, timeout=20)
        if "error" in result:
            raise RuntimeError(f"{method} failed: {result['error']}")
        return result.get("result", {})

    async def navigate(self, url: str) -> None:
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(0.8)

    async def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        result = await self.send("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        })
        res = result.get("result", {})
        if result.get("exceptionDetails"):
            raise RuntimeError(
                f"JS exception: {result['exceptionDetails'].get('text')}: "
                f"{result['exceptionDetails'].get('exception', {}).get('description', '')}"
            )
        return res.get("value")

    async def screenshot(self, path: str) -> str:
        result = await self.send("Page.captureScreenshot", {"format": "png"})
        data = base64.b64decode(result["data"])
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    async def click(self, selector: str) -> None:
        """Real trusted click at the centre of an element."""
        x, y = await self.element_center(selector)
        await self.mouse("mousePressed", x, y)
        await self.mouse("mouseReleased", x, y)

    async def element_center(self, selector: str) -> tuple[float, float]:
        box = await self.evaluate(f"""
            (() => {{
              const el = document.querySelector({json.dumps(selector)});
              if (!el) return null;
              const r = el.getBoundingClientRect();
              return {{x: r.left + r.width / 2, y: r.top + r.height / 2}};
            }})()
        """)
        if not box:
            raise RuntimeError(f"element not found: {selector}")
        return box["x"], box["y"]

    async def mouse(self, event: str, x: float, y: float,
                    button: str = "left", buttons: int = 1, click_count: int = 1,
                    delta_x: float = 0, delta_y: float = 0) -> None:
        await self.send("Input.dispatchMouseEvent", {
            "type": event, "x": x, "y": y, "button": button,
            "buttons": buttons, "clickCount": click_count,
            "deltaX": delta_x, "deltaY": delta_y,
        })

    async def key(self, event: str, key: str, code: str,
                  text: Optional[str] = None, windows_vk: int = 0) -> None:
        params: dict = {
            "type": event, "key": key, "code": code,
            "windowsVirtualKeyCode": windows_vk or (ord(key.upper()) if len(key) == 1 else 0),
            "nativeVirtualKeyCode": windows_vk or (ord(key.upper()) if len(key) == 1 else 0),
        }
        if text is not None:
            params["text"] = text
        await self.send("Input.dispatchKeyEvent", params)

    async def type_key(self, key: str, code: Optional[str] = None,
                       text: Optional[str] = None) -> None:
        code = code or f"Key{key.upper()}" if len(key) == 1 else key
        text = key if (text is None and len(key) == 1) else text
        await self.key("keyDown", key, code, text=text)
        await self.key("keyUp", key, code)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.user_data_dir, ignore_errors=True)
