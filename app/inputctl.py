"""Authorized X11 input injection (mouse, buttons, scroll, keyboard).

Input is injected with the XTEST extension into the X display the customer
application captured.  XTEST synthesises real input events at the X server
level, so they reach whatever application runs on that desktop (Openbox,
Xvfb sessions, normal local desktops).  No X access-control changes and no
grabbing of the X server are involved.

All Xlib calls are funnelled through one worker thread because Xlib
connections are not thread-safe.

Keyboard events use DOM `KeyboardEvent.key` names as sent by the technician
browser ("a", "Enter", "ArrowLeft", "Control", ...); they are mapped to X
keysyms internally.
"""

from __future__ import annotations

import queue
import threading
from typing import Optional

from .displayutil import parse_display

# DOM KeyboardEvent.key -> X keysym name
_KEY_MAP = {
    "Enter": "Return",
    "Backspace": "BackSpace",
    "Escape": "Escape",
    "Tab": "Tab",
    " ": "space",
    "Spacebar": "space",
    "ArrowLeft": "Left",
    "ArrowRight": "Right",
    "ArrowUp": "Up",
    "ArrowDown": "Down",
    "Delete": "Delete",
    "Home": "Home",
    "End": "End",
    "PageUp": "Page_Up",
    "PageDown": "Page_Down",
    "Insert": "Insert",
    "Shift": "Shift_L",
    "Control": "Control_L",
    "Alt": "Alt_L",
    "Meta": "Super_L",
    "CapsLock": "Caps_Lock",
    "NumLock": "Num_Lock",
    "ScrollLock": "Scroll_Lock",
    "PrintScreen": "Print",
    "ContextMenu": "Menu",
}
for _i in range(1, 13):
    _KEY_MAP[f"F{_i}"] = f"F{_i}"

_BUTTONS = {"left": 1, "middle": 2, "right": 3}
_SCROLL_UP, _SCROLL_DOWN, _SCROLL_LEFT, _SCROLL_RIGHT = 4, 5, 6, 7


class InputError(RuntimeError):
    pass


def keysym_for(key: str) -> int:
    """Map a DOM key name to an X keysym (0 when unknown)."""
    from Xlib import XK

    if key in _KEY_MAP:
        return XK.string_to_keysym(_KEY_MAP[key])
    if len(key) == 1:
        sym = XK.string_to_keysym(key)
        if sym:
            return sym
        if key.isupper():
            return XK.string_to_keysym(key.lower())
    # Last resort: try the name verbatim (e.g. "XF86AudioPlay").
    return XK.string_to_keysym(key)


class InputController:
    def __init__(self, display: str) -> None:
        self.display_raw = display
        from .displayutil import verify_display

        info = verify_display(display)
        self._display_name = info["xlib_name"]
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._ready = threading.Event()
        self._failed: Optional[str] = None
        self._thread = threading.Thread(
            target=self._worker, name="x11-input", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise InputError(
                f"input controller could not connect to display {display!r}: "
                f"{self._failed or 'timeout'}"
            )
        if self._failed:
            raise InputError(
                f"input controller could not connect to display {display!r}: "
                f"{self._failed}"
            )

    # ------------------------------------------------------------ public API

    def pointer_move(self, x: int, y: int) -> None:
        self._queue.put(("move", int(x), int(y)))

    def button(self, button: str, pressed: bool) -> None:
        if button not in _BUTTONS:
            raise InputError(f"unknown mouse button {button!r}")
        self._queue.put(("button", _BUTTONS[button], bool(pressed)))

    def scroll(self, dx: float, dy: float) -> None:
        self._queue.put(("scroll", float(dx), float(dy)))

    def key(self, key: str, pressed: bool) -> None:
        self._queue.put(("key", str(key), bool(pressed)))

    def wait_idle(self, timeout: float = 2.0) -> bool:
        """Block until queued events are processed (used by tests)."""
        done = threading.Event()
        self._queue.put(("sync", done))
        return done.wait(timeout)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=3.0)

    # --------------------------------------------------------------- worker

    def _worker(self) -> None:
        try:
            from Xlib import X, display as xdisplay
            from Xlib.ext import xtest

            d = xdisplay.Display(self._display_name)
        except Exception as exc:
            self._failed = str(exc)
            self._ready.set()
            return
        self._ready.set()

        def press(event_type, detail) -> None:
            xtest.fake_input(d, event_type, detail)
            d.sync()

        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                op = item[0]
                try:
                    if op == "move":
                        xtest.fake_input(d, X.MotionNotify, x=item[1], y=item[2])
                        d.sync()
                    elif op == "button":
                        detail = item[1]
                        event = X.ButtonPress if item[2] else X.ButtonRelease
                        press(event, detail)
                    elif op == "scroll":
                        dx, dy = item[1], item[2]
                        # A "click" of wheel = one press+release of button 4/5.
                        for _ in range(int(round(abs(dy)))):
                            btn = _SCROLL_UP if dy > 0 else _SCROLL_DOWN
                            press(X.ButtonPress, btn)
                            press(X.ButtonRelease, btn)
                        for _ in range(int(round(abs(dx)))):
                            btn = _SCROLL_RIGHT if dx > 0 else _SCROLL_LEFT
                            press(X.ButtonPress, btn)
                            press(X.ButtonRelease, btn)
                    elif op == "key":
                        key, pressed = item[1], item[2]
                        sym = keysym_for(key)
                        if not sym:
                            continue
                        code = d.keysym_to_keycode(sym)
                        if not code:
                            continue
                        # Uppercase printable characters need Shift.
                        needs_shift = len(key) == 1 and key.isupper() and key.isalpha()
                        shift_code = d.keysym_to_keycode(
                            keysym_for("Shift")
                        ) if needs_shift else 0
                        if needs_shift:
                            press(X.KeyPress, shift_code)
                        press(X.KeyPress if pressed else X.KeyRelease, code)
                        if needs_shift and not pressed:
                            press(X.KeyRelease, shift_code)
                    elif op == "sync":
                        item[1].set()
                except Exception:
                    # Keep the worker alive; a failed injection must not kill
                    # the session loop.  Callers surface control state in UI.
                    pass
        finally:
            try:
                d.close()
            except Exception:
                pass


def query_pointer(display: str) -> tuple[int, int]:
    """Read the current pointer position on a display (for tests/diagnostics)."""
    from .displayutil import verify_display

    info = verify_display(display)
    from Xlib import display as xdisplay

    d = xdisplay.Display(info["xlib_name"])
    try:
        ptr = d.screen().root.query_pointer()
        return int(ptr.root_x), int(ptr.root_y)
    finally:
        d.close()
