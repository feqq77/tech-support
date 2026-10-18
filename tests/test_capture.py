"""Screen capture of the real desktop pixels on a virtual X display."""

from __future__ import annotations

import time

import pytest

from app.capture import CaptureError, ScreenCapturer
from tests.conftest import draw_window


def _pixel_at(frame: bytes, geom: dict, x: int, y: int) -> tuple[int, int, int]:
    """Read an RGB pixel out of a BGRA frame buffer."""
    w = geom["width"]
    off = (y * w + x) * 4
    b, g, r = frame[off], frame[off + 1], frame[off + 2]
    return r, g, b


def test_capture_real_pixels(xvfb_display):
    color = (200, 30, 90)
    close, _win, _d = draw_window(xvfb_display, 100, 100, 300, 200, color)
    try:
        cap = ScreenCapturer(xvfb_display, fps=10)
        info = cap.prepare()
        assert info["width"] == 1280 and info["height"] == 800
        cap.start()
        deadline = time.time() + 5
        frame, geom, seq = None, None, 0
        while time.time() < deadline:
            frame, geom, seq = cap.latest()
            if frame and seq > 2:
                break
            time.sleep(0.1)
        assert frame is not None, "no frame captured"
        assert geom["width"] * geom["height"] * 4 == len(frame)

        # The exact pixels of our window must be visible in the captured frame.
        got = _pixel_at(frame, geom, 250, 200)
        assert got == color, f"expected {color}, captured {got}"
        # And outside the window the root background stays black.
        assert _pixel_at(frame, geom, 1100, 700) == (0, 0, 0)
    finally:
        cap.stop()
        close()


def test_capture_updates(xvfb_display):
    cap = ScreenCapturer(xvfb_display, fps=10)
    cap.prepare()
    cap.start()
    try:
        time.sleep(0.4)
        _, _, seq1 = cap.latest()
        time.sleep(0.5)
        _, _, seq2 = cap.latest()
        assert seq2 > seq1, "capturer is not producing new frames"
    finally:
        cap.stop()


def test_geometry_reports_root_rect(xvfb_display):
    cap = ScreenCapturer(xvfb_display)
    cap.prepare()
    geom = cap.geometry()
    assert geom == {"left": 0, "top": 0, "width": 1280, "height": 800}
    cap.stop()


def test_capture_wrong_display_fails_loudly(xvfb_display):
    cap = ScreenCapturer("127.0.0.1:199", fps=5)
    with pytest.raises((CaptureError, Exception)) as exc:
        cap.prepare()
    assert "199" in str(exc.value)


def test_invalid_fps_rejected():
    with pytest.raises(CaptureError):
        ScreenCapturer(":0", fps=0)
    with pytest.raises(CaptureError):
        ScreenCapturer(":0", fps=120)
