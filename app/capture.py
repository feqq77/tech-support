"""Real screen capture.

Captures the actual pixels of the *selected* desktop.  Nothing here assumes
DISPLAY == :0.

Backends:
  * pipewire - compositor capture via the session's ScreenCast/PipeWire API
              (app/pipewiregrab.py, run with the system python3).  This is the
              only honest capture path when the compositor keeps the pixels
              off the X screen (XWayland, or NVIDIA's GLX compositor leaving
              the root grab pure black).
  * mss     - XShm based grabber (default on ordinary X11, in the app venv)
  * ffmpeg  - x11grab subprocess fallback, used when mss cannot initialise

A background thread captures at a fixed rate and keeps only the newest frame,
so the WebRTC encoder always reads a fresh frame without queueing latency.
Frames are delivered as raw BGRA bytes which map 1:1 onto av.VideoFrame, so
no numpy dependency is needed.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
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
        self._auto = backend == "auto"
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
        self._frame_bytes = 0
        self._prefetched = b""
        self._virtual = False

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
        if self.backend_name == "pipewire":
            try:
                self._init_pipewire()
                return info
            except Exception as exc:
                raise CaptureError(f"compositor capture failed: {exc}") from exc
        if self.backend_name in ("auto", "mss"):
            try:
                self._init_mss(conn_name)
            except Exception as exc:
                errors.append(f"mss backend failed: {exc}")
                if self.backend_name == "mss":
                    raise CaptureError("; ".join(errors)) from exc
            else:
                if self.backend_name == "mss":
                    self.backend_name = "mss"
                    return info
                # auto: an all-zero grab is the classic symptom of a compositor
                # keeping the pixels off the X screen (XWayland, or NVIDIA's
                # GLX compositor).  On real session desktops escalate to the
                # compositor's own stream - and never to ffmpeg, which reads
                # the same blank root window.  On virtual displays (Xvfb, ...)
                # all-black is simply the truth, so the X11 grab stands.
                if self._virtual or not self._grab_looks_black():
                    self.backend_name = "mss"
                    return info
                errors.append(
                    "X11 grab of this desktop returns only black pixels "
                    "(the compositor keeps them off the X screen)"
                )
                try:
                    self._init_pipewire()
                    self.backend_name = "pipewire"
                    return info
                except Exception as exc:
                    errors.append(f"compositor capture failed: {exc}")
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
        # auto with both X11 grabbers unavailable: last chance is the compositor
        if self.backend_name == "auto":
            try:
                self._init_pipewire()
                self.backend_name = "pipewire"
                return info
            except Exception as exc:
                errors.append(f"compositor capture failed: {exc}")
        raise CaptureError(
            f"could not initialise screen capture on {self.display_raw!r}: "
            + "; ".join(errors)
        )

    def _grab_looks_black(self, attempts: int = 5, gap: float = 0.12) -> bool:
        """True when the grabber returns nothing but zeros, grab after grab.

        With NVIDIA's GLX compositor (and on XWayland) the root window
        technically "works" but carries no pixels: every byte is 0 even though
        the desktop has content.  Streaming that is the black-screen bug.
        Several attempts with gaps: some drivers only start populating the
        root pixmap after a few grabs.
        """
        for i in range(attempts):
            try:
                shot = self._sct.grab(self._monitor)
                data = bytes(shot.raw)
            except Exception:
                return False  # broken grab, not blank: keep the normal path
            if not data:
                return True
            if max(data) != 0:
                return False
            if i + 1 < attempts:
                self._stop.wait(gap)
        return True

    def _is_virtual_display(self) -> bool:
        """True when this display belongs to a virtual X server (Xvfb, ...).

        Those are started explicitly to be captured; an all-black frame there
        is real content, not a broken grabber, so we never escalate it.
        """
        m = re.search(r":(\d+)", self.display_raw or "")
        if not m:
            return False
        want = ":" + m.group(1)
        names = {"Xvfb", "Xvnc", "Xephyr", "Xnest", "Xdummy"}
        try:
            pids = os.listdir("/proc")
        except OSError:
            return False
        for pid in pids:
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    parts = fh.read().split(b"\0")
            except OSError:
                continue
            if not parts or not parts[0]:
                continue
            exe = os.path.basename(parts[0].decode(errors="replace"))
            if exe not in names:
                continue
            for arg in parts[1:]:
                a = arg.decode(errors="replace")
                if a == want or a == want + ".0":
                    return True
        return False

    @staticmethod
    def _system_python() -> Optional[str]:
        """Find a python3 that has PyGObject + GStreamer (the app venv has neither)."""
        candidates = ["/usr/bin/python3", "/usr/local/bin/python3"]
        which = shutil.which("python3")
        if which:
            candidates.append(which)
        seen: set[str] = set()
        for cand in candidates:
            cand = os.path.abspath(cand)
            if cand in seen or not os.path.isfile(cand):
                continue
            seen.add(cand)
            try:
                probe = subprocess.run(
                    [
                        cand,
                        "-c",
                        "import gi; gi.require_version('Gst', '1.0'); "
                        "from gi.repository import Gst",
                    ],
                    capture_output=True,
                    timeout=10,
                )
                if probe.returncode == 0:
                    return cand
            except Exception:
                continue
        return None

    def _init_pipewire(self) -> None:
        helper = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "pipewiregrab.py"
        )
        if not os.path.isfile(helper):
            raise CaptureError("compositor capture helper is missing from this build")
        python = self._system_python()
        if not python:
            raise CaptureError(
                "no system python3 with PyGObject/GStreamer available "
                "(needed for compositor capture)"
            )
        self._proc = subprocess.Popen(
            [python, helper, "--fps", str(self.fps)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # Header line first: {"width": W, "height": H, "monitor": ...}
        deadline = time.monotonic() + 15.0
        header = b""
        while b"\n" not in header:
            remain = deadline - time.monotonic()
            if remain <= 0:
                self._kill_helper()
                raise CaptureError("compositor capture helper produced no header")
            ready, _, _ = select.select([self._proc.stdout], [], [], remain)
            if not ready:
                self._kill_helper()
                raise CaptureError("compositor capture helper produced no header")
            chunk = os.read(self._proc.stdout.fileno(), 4096)
            if not chunk:
                err = self._helper_stderr()
                raise CaptureError(
                    "compositor capture helper exited: " + (err or "no output")
                )
            header += chunk
        line, rest = header.split(b"\n", 1)
        try:
            meta = json.loads(line.decode())
            width, height = int(meta["width"]), int(meta["height"])
        except Exception as exc:
            self._kill_helper()
            raise CaptureError(
                f"bad compositor helper header {line!r}: {exc}"
            ) from exc
        if width <= 0 or height <= 0:
            self._kill_helper()
            raise CaptureError(f"compositor stream reported no size: {width}x{height}")
        self._frame_bytes = width * height * 4
        self._prefetched = rest  # bytes already read past the header line
        with self._lock:
            self._geom = {"left": 0, "top": 0, "width": width, "height": height}

    def _helper_stderr(self) -> str:
        try:
            if self._proc is not None and self._proc.stderr is not None:
                self._proc.poll()
                data = b""
                # Bounded drain: only read what is already available so a live
                # process can never block us here.
                while True:
                    ready, _, _ = select.select([self._proc.stderr], [], [], 0.5)
                    if not ready:
                        break
                    chunk = os.read(self._proc.stderr.fileno(), 4096)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > 65536:
                        break
                return data.decode(errors="replace").strip()
        except Exception:
            pass
        return ""

    def _kill_helper(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

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
        self._virtual = self._is_virtual_display()
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
        elif self.backend_name == "pipewire":
            self._run_stream("compositor capture stopped: ")
        else:
            self._run_stream("ffmpeg capture stopped: ")

    def _run_mss(self, interval: float) -> None:
        next_t = time.monotonic()
        blank_since: Optional[float] = None
        while not self._stop.is_set():
            try:
                shot = self._sct.grab(self._monitor)
                data = bytes(shot.raw)
            except Exception as exc:
                self._error = f"screen grab failed: {exc}"
                time.sleep(0.2)
                continue
            # Self-heal: compositor/driver state can flip mid-session and start
            # returning blank frames (seen with NVIDIA GLX).  After ~3s of
            # blanks on a real desktop, move the stream to the compositor's
            # own capture; geometry is identical so the video track keeps
            # running uninterrupted.  Auto mode only, once per blank period.
            if self._auto and not self._virtual and data and max(data) == 0:
                now = time.monotonic()
                if blank_since is None:
                    blank_since = now
                elif now - blank_since > 3.0:
                    blank_since = now  # pace retries
                    try:
                        self._init_pipewire()
                        self.backend_name = "pipewire"
                        try:
                            self._sct.close()
                        except Exception:
                            pass
                        self._sct = None
                        self._run_stream("compositor capture stopped: ")
                        return
                    except Exception as exc:
                        self._error = (
                            "blank X11 frames; compositor capture failed: " + str(exc)
                        )
            else:
                blank_since = None
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

    def _run_stream(self, err_prefix: str) -> None:
        size = self._frame_bytes
        out = self._proc.stdout
        pending = self._prefetched or b""
        self._prefetched = b""
        while not self._stop.is_set():
            data = self._read_exact(out, size, pending)
            pending = b""
            if not data or len(data) < size:
                if not self._stop.is_set():
                    err = self._helper_stderr()
                    self._error = err_prefix + (err or "end of frame stream")
                break
            with self._lock:
                self._frame = data
                self._frame_seq += 1
                self._frame_ts = time.time()

    @staticmethod
    def _read_exact(out, size: int, prefix: bytes = b"") -> bytes:
        """Read exactly size bytes (raw pipes may return short reads)."""
        buf = bytearray(prefix)
        while len(buf) < size:
            chunk = out.read(size - len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)
