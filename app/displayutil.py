"""X11 display detection, normalization and verification.

Nothing in this module assumes DISPLAY is ":0".  Displays such as

    :0
    :106
    127.0.0.1:106
    127.0.0.1:106.0
    localhost:10.0

are all valid forms.  We parse them explicitly, verify that an X server is
actually reachable before any capture starts, and produce real error messages
when it is not.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

# host:display.screen -- host may be empty (local unix socket), an IP or name.
_DISPLAY_RE = re.compile(
    r"^(?:(?P<host>[A-Za-z0-9._\-]*):)?(?P<num>\d+)(?:\.(?P<screen>\d+))?$"
)


class DisplayError(RuntimeError):
    """Raised when an X display cannot be parsed or reached."""


@dataclass(frozen=True)
class ParsedDisplay:
    host: str  # "" means local unix socket
    num: int
    screen: int
    raw: str

    @property
    def xlib_name(self) -> str:
        """String accepted by XOpenDisplay / python-xlib (always includes screen)."""
        return f"{self.host}:{self.num}.{self.screen}"

    @property
    def env_name(self) -> str:
        """String accepted in $DISPLAY by most clients (screen optional)."""
        host = self.host
        return f"{host}:{self.num}"

    @property
    def socket_path(self) -> str | None:
        """Unix socket path for purely local displays, else None."""
        if self.host in ("", "unix"):
            return f"/tmp/.X11-unix/X{self.num}"
        return None


def parse_display(raw: str | None) -> ParsedDisplay:
    if raw is None:
        raise DisplayError(
            "DISPLAY is not set. Pass --display (e.g. --display :106 or "
            "--display 127.0.0.1:106) or export DISPLAY."
        )
    s = raw.strip()
    if not s:
        raise DisplayError("DISPLAY is set but empty.")
    m = _DISPLAY_RE.match(s)
    if not m:
        raise DisplayError(
            f"Cannot parse display {raw!r}. Expected forms like ':0', ':106', "
            "'127.0.0.1:106' or '127.0.0.1:106.0'."
        )
    return ParsedDisplay(
        host=m.group("host") or "",
        num=int(m.group("num")),
        screen=int(m.group("screen") or 0),
        raw=s,
    )


def detect_display(preferred: str | None = None) -> ParsedDisplay:
    """Choose the display to use: explicit arg > $DISPLAY > hard error."""
    if preferred:
        return parse_display(preferred)
    env = os.environ.get("DISPLAY")
    if env:
        return parse_display(env)
    raise DisplayError(
        "No X display selected: DISPLAY is not set and no --display was given. "
        f"Displays currently present on this machine: "
        f"{', '.join(list_local_displays()) or '(none found)'}."
    )


def list_local_displays() -> list[str]:
    """Display numbers with a local X socket in /tmp/.X11-unix."""
    names = []
    sockdir = "/tmp/.X11-unix"
    try:
        entries = sorted(os.listdir(sockdir))
    except OSError:
        return names
    for entry in entries:
        if entry.startswith("X") and entry[1:].isdigit():
            names.append(f":{entry[1:]}")
    return names


def _verify_with_xdpyinfo(parsed: ParsedDisplay, timeout: float) -> dict:
    out = subprocess.run(
        ["xdpyinfo", "-display", parsed.raw],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if out.returncode != 0:
        raise DisplayError(
            f"xdpyinfo cannot connect to display {parsed.raw!r}: "
            f"{(out.stderr or out.stdout).strip() or 'unknown error'}"
        )
    width = height = depth = None
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("dimensions:"):
            m = re.match(r"dimensions:\s+(\d+)x(\d+)\s+pixels", line)
            if m:
                width, height = int(m.group(1)), int(m.group(2))
        elif line.startswith("depth of root window:"):
            m = re.match(r"depth of root window:\s+(\d+)\s+planes", line)
            if m:
                depth = int(m.group(1))
    return {"width": width, "height": height, "depth": depth, "method": "xdpyinfo"}


def _verify_with_xlib(parsed: ParsedDisplay) -> dict:
    from Xlib import display as xdisplay  # imported lazily

    try:
        d = xdisplay.Display(parsed.xlib_name)
    except Exception as exc:  # Xlib raises various error types
        raise DisplayError(
            f"cannot connect to display {parsed.raw!r} via Xlib: {exc}"
        ) from exc
    try:
        screen = d.screen()
        return {
            "width": int(screen.width_in_pixels),
            "height": int(screen.height_in_pixels),
            "depth": int(screen.root_depth),
            "method": "xlib",
        }
    finally:
        try:
            d.close()
        except Exception:
            pass


LOOPBACK_HOSTS = ("", "unix", "127.0.0.1", "localhost", "::1")


def _probe(parsed: ParsedDisplay, timeout: float) -> dict:
    if shutil.which("xdpyinfo"):
        try:
            info = _verify_with_xdpyinfo(parsed, timeout)
        except subprocess.TimeoutExpired as exc:
            raise DisplayError(
                f"display {parsed.raw!r} did not answer within {timeout:g}s "
                "(xdpyinfo timed out)."
            ) from exc
    else:
        info = _verify_with_xlib(parsed)
    if not info.get("width") or not info.get("height"):
        raise DisplayError(
            f"display {parsed.raw!r} answered but did not report a usable size."
        )
    return info


def verify_display(raw: str, timeout: float = 5.0) -> dict:
    """Verify an X display is reachable and describe it.

    Returns {"display", "xlib_name", "env_name", "transport", "width",
    "height", "depth", "method"} or raises DisplayError explaining the actual
    failure.

    For loopback displays such as "127.0.0.1:106" the X server often only
    listens on the local unix socket (Xvfb defaults to "-nolisten tcp").  When
    the TCP form is unreachable but the *same* display number has a local
    socket, we transparently use that socket and report the transport that
    actually worked.  No X access controls are changed.
    """
    parsed = parse_display(raw)
    local_socket = f"/tmp/.X11-unix/X{parsed.num}"

    try:
        info = _probe(parsed, timeout)
        info["transport"] = "unix-socket" if parsed.host in ("", "unix") else "tcp"
        info["display"] = parsed.raw
        info["xlib_name"] = parsed.xlib_name
        info["env_name"] = parsed.env_name
        return info
    except DisplayError as first_error:
        # Transparent fallback: same display number via its local unix socket.
        if parsed.host in LOOPBACK_HOSTS and os.path.exists(local_socket):
            alt = ParsedDisplay(
                host="", num=parsed.num, screen=parsed.screen,
                raw=f":{parsed.num}.{parsed.screen}",
            )
            try:
                info = _probe(alt, timeout)
            except DisplayError:
                alt2 = ParsedDisplay(
                    host="", num=parsed.num, screen=0,
                    raw=f":{parsed.num}.0",
                )
                try:
                    info = _probe(alt2, timeout)
                    alt = alt2
                except DisplayError:
                    raise first_error
            info["transport"] = "unix-socket"
            info["display"] = parsed.raw
            info["xlib_name"] = alt.xlib_name
            info["env_name"] = alt.env_name
            info["note"] = (
                f"display {parsed.raw!r} is not reachable over TCP but the same "
                f"display number answered via local socket {local_socket}; "
                "using the socket (start the X server with '-listen tcp' if the "
                "TCP form is required)."
            )
            return info

        # Nothing worked: explain precisely.
        details = [str(first_error)]
        if parsed.host in LOOPBACK_HOSTS:
            if not os.path.exists(local_socket):
                details.append(
                    f"no local unix socket {local_socket} exists either. Start an "
                    f"X server for this display (example: Xvfb :{parsed.num} "
                    f"-screen 0 1280x800x24 -listen tcp &) or choose another "
                    "display. Local displays currently present: "
                    f"{', '.join(list_local_displays()) or '(none)'}."
                )
            elif parsed.host not in ("", "unix"):
                details.append(
                    f"a local unix socket {local_socket} exists but could not be "
                    "used; the X server may require '-listen tcp' for "
                    f"{parsed.raw!r} access."
                )
        raise DisplayError(" ".join(details))
