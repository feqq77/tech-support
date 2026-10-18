"""Input injection verified against real X events on a virtual display."""

from __future__ import annotations

import queue
import threading
import time

import pytest

from app.inputctl import InputController, keysym_for, query_pointer
from tests.conftest import draw_window


class EventPump:
    """Collects X events from a window in a background thread."""

    def __init__(self, display: str, x: int, y: int, w: int, h: int):
        from Xlib import X, display as xdisplay

        self.d = xdisplay.Display(display)
        screen = self.d.screen()
        self.win = screen.root.create_window(
            x, y, w, h, 0, screen.root_depth, X.InputOutput, X.CopyFromParent,
            background_pixel=0x00FF00,
            event_mask=(X.KeyPressMask | X.KeyReleaseMask | X.ButtonPressMask
                        | X.ButtonReleaseMask),
        )
        self.win.map()
        self.d.sync()
        # Wait until the window is actually viewable so injected clicks cannot
        # land on the root window while the server is still processing the map.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self.win.get_attributes().map_state == X.IsViewable:
                break
            time.sleep(0.05)
        time.sleep(0.1)
        self.events: "queue.Queue" = queue.Queue()
        self.pump_error: str | None = None
        self.pointer_child: int | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        while not self._stop.is_set():
            try:
                ev = self.d.next_event()
                self.events.put(ev)
            except Exception as exc:  # pragma: no cover - diagnostics only
                self.pump_error = str(exc)
                break

    def ensure_under_pointer(self, timeout: float = 5.0) -> bool:
        """Raise the window and wait until it is the top-level under the pointer.

        Button events go to the window under the pointer; if some stale window
        is stacked above ours (leftover from an earlier test or process), the
        clicks would never reach us.  Raising + verifying makes the click tests
        deterministic and self-diagnosing.
        """
        from Xlib import X

        deadline = time.time() + timeout
        while time.time() < deadline:
            self.win.configure(stack_mode=X.Above)
            self.d.sync()
            rep = self.d.screen().root.query_pointer()
            child = getattr(rep.child, "id", None)
            self.pointer_child = child
            if child == self.win.id:
                return True
            time.sleep(0.05)
        return False

    def wait_for(self, predicate, timeout: float = 5.0):
        deadline = time.time() + timeout
        found = []
        while time.time() < deadline:
            try:
                ev = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            if predicate(ev):
                return ev
            found.append(ev)
        return None

    def close(self):
        self._stop.set()
        try:
            self.d.close()
        except Exception:
            pass


@pytest.fixture()
def input_target(xvfb_display):
    pump = EventPump(xvfb_display, 100, 100, 300, 200)
    yield xvfb_display, pump
    pump.close()


def test_pointer_move_exact(input_target):
    display, _ = input_target
    ic = InputController(display)
    try:
        ic.pointer_move(640, 400)
        assert ic.wait_idle()
        assert query_pointer(display) == (640, 400)
        ic.pointer_move(0, 0)
        assert ic.wait_idle()
        assert query_pointer(display) == (0, 0)
        ic.pointer_move(1279, 799)
        assert ic.wait_idle()
        assert query_pointer(display) == (1279, 799)
    finally:
        ic.close()


def _aim(pump: EventPump, ic: "InputController", x: int, y: int) -> None:
    """Move the pointer to (x, y) and ensure the test window is under it."""
    ic.pointer_move(x, y)
    assert ic.wait_idle()
    assert pump.ensure_under_pointer(), (
        f"pointer is over top-level window {pump.pointer_child}, "
        f"not the test window {pump.win.id}"
    )


def test_mouse_button_events_reach_window(input_target):
    from Xlib import X

    display, pump = input_target
    ic = InputController(display)
    try:
        _aim(pump, ic, 250, 200)
        ic.button("left", True)
        ic.button("left", False)
        ic.button("right", True)
        ic.button("right", False)
        assert ic.wait_idle()
        ev = pump.wait_for(lambda e: e.type == X.ButtonPress and e.detail == 1)
        assert ev is not None, (
            f"left click never reached the window (pump error: {pump.pump_error})"
        )
        ev = pump.wait_for(lambda e: e.type == X.ButtonPress and e.detail == 3)
        assert ev is not None, (
            f"right click never reached the window (pump error: {pump.pump_error})"
        )
    finally:
        ic.close()


def test_scroll_generates_wheel_clicks(input_target):
    from Xlib import X

    display, pump = input_target
    ic = InputController(display)
    try:
        _aim(pump, ic, 250, 200)
        ic.scroll(0, 1)   # wheel up  -> button 4
        ic.scroll(0, -1)  # wheel down -> button 5
        assert ic.wait_idle()
        assert pump.wait_for(lambda e: e.type == X.ButtonPress and e.detail == 4)
        assert pump.wait_for(lambda e: e.type == X.ButtonPress and e.detail == 5)
    finally:
        ic.close()


def test_keyboard_events_reach_window(input_target):
    from Xlib import XK, X, display as xdisplay

    display, pump = input_target
    probe = xdisplay.Display(pump.d.get_display_name())
    a_code = probe.keysym_to_keycode(XK.string_to_keysym("a"))
    ret_code = probe.keysym_to_keycode(XK.string_to_keysym("Return"))
    probe.close()

    ic = InputController(display)
    try:
        _aim(pump, ic, 250, 200)
        ic.key("a", True)
        ic.key("a", False)
        ic.key("Enter", True)
        ic.key("Enter", False)
        assert ic.wait_idle()

        assert pump.wait_for(
            lambda e: e.type == X.KeyPress and e.detail == a_code
        ), "'a' keypress never reached the window"
        assert pump.wait_for(
            lambda e: e.type == X.KeyPress and e.detail == ret_code
        ), "Enter keypress never reached the window"
    finally:
        ic.close()


def test_uppercase_key_uses_shift(input_target):
    from Xlib import XK, X, display as xdisplay

    display, pump = input_target
    probe = xdisplay.Display(pump.d.get_display_name())
    shift_code = probe.keysym_to_keycode(XK.string_to_keysym("Shift_L"))
    probe.close()

    ic = InputController(display)
    try:
        _aim(pump, ic, 250, 200)
        ic.key("A", True)
        ic.key("A", False)
        assert ic.wait_idle()
        shift_down = pump.wait_for(
            lambda e: e.type == X.KeyPress and e.detail == shift_code
        )
        assert shift_down is not None, "Shift was not pressed for uppercase key"
    finally:
        ic.close()


def test_keysym_mapping():
    assert keysym_for("Enter") != 0
    assert keysym_for("ArrowLeft") != 0
    assert keysym_for("F5") != 0
    assert keysym_for("Control") != 0
    assert keysym_for("a") != 0
    assert keysym_for("A") == keysym_for("a") or keysym_for("A") != 0


def test_unknown_button_rejected(xvfb_display):
    ic = InputController(xvfb_display)
    try:
        with pytest.raises(Exception):
            ic.button("sideways", True)
    finally:
        ic.close()
