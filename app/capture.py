"""Real X11 screen capture.

Captures the actual pixels of the *selected* X display (local, virtual Xvfb
displays such as 127.0.0.1:106, ...).  Nothing here assumes DISPLAY == :0.

Backends:
  * mss  - XShm based grabber (default, installed in the app venv)
  * ffmpeg - x11grab subprocess fallback, used when mss cannot initialise

A background thread captures at a fixed rate and keeps only the newest frame,
so the WebRTC encoder always reads a fresh frame without queueing latency.
Frames are delivered as raw BGRA bytes which map 1:1 onto av.VideoFrame, so
no numpy dependency is needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Optional

from .displayutil import DisplayError, parse_display, verify_display


class CaptureError(RuntimeError):
    pass


class ScreenCapturer:
    """Capture frames from one X display.

    geometry() reports the root-window rectangle that is captured
    (left/top/width/height) so pointer coordinates can be mapped back to the
    correct spot on the desktop even when the root origin is not (0, 0).
    """

    def __init__(
        self,
        display: str,
        fps: float = 8.0,
        backend: str = "auto",
        verify_timeout: float = 5.0,
    ) -> None:
        if fps <= 0 or fps > 60:
            raise CaptureError(f"fps must be in (0, 60], got {fps}")
        self.display_raw = display
        self.fps = fps
        self.backend_name = backend
        self._verify_timeout = verify_timeout

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frame: Optional[bytes] = None
        self._frame_seq = 0
        self._frame_ts = 0.0
        self._geom = {"left": 0, "top": 0, "width": 0, "height": 0}
        self._error: Optional[str] = None

        # backend handles
        self._sct = None
        self._monitor = None
        self._proc: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------ setup

    def prepare(self) -> dict:
        """Verify the display and initialise the capture backend.

        Raises CaptureError/DisplayError with the *actual* failure otherwise.
        """
        parsed = parse_display(self.display_raw)
        info = verify_display(self.display_raw, timeout=self._verify_timeout)
        # Use the connection string that actually verified (e.g. the local unix
        # socket fallback for 127.0.0.1:106 style displays), and make sure any
        # library reading $DISPLAY talks to the display the operator selected.
        conn_name = info["xlib_name"]
        os.environ["DISPLAY"] = info["env_name"]

        errors: list[str] = []
        if self.backend_name in ("auto", "mss"):
            try:
                self._init_mss(conn_name)
                self.backend_name = "mss"
                return info
            except Exception as exc:
                errors.append(f"mss backend failed: {exc}")
                if self.backend_name == "mss":
                    raise CaptureError("; ".join(errors)) from exc
        if self.backend_name in ("auto", "ffmpeg"):
            try:
                self._init_ffmpeg(conn_name, info)
                self.backend_name = "ffmpeg"
                return info
            except Exception as exc:
                errors.append(f"ffmpeg backend failed: {exc}")
                if self.backend_name == "ffmpeg":
                    raise CaptureError("; ".join(errors)) from exc
        raise CaptureError(
            f"could not initialise screen capture on {self.display_raw!r}: "
            + "; ".join(errors)
        )

    def _init_mss(self, conn_name: str) -> None:
        import mss

        try:
            sct = mss.MSS(display=conn_name)
        except TypeError:
            sct = mss.MSS()
        except AttributeError:
            sct = mss.mss(display=conn_name)
        try:
            monitor = sct.monitors[0]  # full root window of this X display
            if not monitor or monitor["width"] <= 0 or monitor["height"] <= 0:
                raise CaptureError(f"no usable monitor on display {parsed.raw!r}")
            # Prove we can actually grab pixels before declaring success.
            shot = sct.grab(monitor)
            if not shot.raw:
                raise CaptureError("grab returned no pixels")
        except Exception:
            try:
                sct.close()
            except Exception:
                pass
            raise
        self._sct = sct
        self._monitor = dict(monitor)
        with self._lock:
            self._geom = {
                "left": int(monitor["left"]),
                "top": int(monitor["top"]),
                "width": int(monitor["width"]),
                "height": int(monitor["height"]),
            }

    def _init_ffmpeg(self, conn_name: str, info: dict) -> None:
        if not shutil.which("ffmpeg"):
            raise CaptureError("ffmpeg is not installed")
        width, height = info["width"], info["height"]
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "x11grab",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            str(self.fps),
            "-i",
            conn_name,
            "-pix_fmt",
            "bgra",
            "-f",
            "rawvideo",
            "-",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=width * height * 4 * 2,
        )
        self._frame_bytes = width * height * 4
        with self._lock:
            self._geom = {"left": 0, "top": 0, "width": width, "height": height}

    # ------------------------------------------------------------------ frames

    def geometry(self) -> dict:
        with self._lock:
            return dict(self._geom)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self.backend_name == "auto":
            self.prepare()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="screen-capture", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                pass
            self._proc = None

    def latest(self) -> tuple[Optional[bytes], dict, int]:
        """Return (frame bytes in BGRA, geometry, sequence number)."""
        with self._lock:
            return self._frame, dict(self._geom), self._frame_seq

    def error(self) -> Optional[str]:
        return self._error

    # ------------------------------------------------------------------ thread

    def _run(self) -> None:
        interval = 1.0 / self.fps
        if self.backend_name == "mss":
            self._run_mss(interval)
        else:
            self._run_ffmpeg()

    def _run_mss(self, interval: float) -> None:
        next_t = time.monotonic()
        while not self._stop.is_set():
            try:
                shot = self._sct.grab(self._monitor)
                data = bytes(shot.raw)
            except Exception as exc:
                self._error = f"screen grab failed: {exc}"
                time.sleep(0.2)
                continue
            with self._lock:
                self._frame = data
                self._frame_seq += 1
                self._frame_ts = time.time()
            next_t += interval
            delay = next_t - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_t = time.monotonic()

    def _run_ffmpeg(self) -> None:
        size = self._frame_bytes
        out = self._proc.stdout
        while not self._stop.is_set():
            data = out.read(size)
            if not data or len(data) < size:
                if not self._stop.is_set():
                    err = b""
                    if self._proc.stderr:
                        err = self._proc.stderr.read() or b""
                    self._error = (
                        "ffmpeg capture stopped: "
                        + err.decode(errors="replace").strip()
                    )
                break
            with self._lock:
                self._frame = data
                self._frame_seq += 1
                self._frame_ts = time.time()
