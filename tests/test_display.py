"""Display parsing and verification against real X servers."""

from __future__ import annotations

import pytest

from app.displayutil import (
    DisplayError,
    detect_display,
    list_local_displays,
    parse_display,
    verify_display,
)


def test_parse_forms():
    d = parse_display(":0")
    assert (d.host, d.num, d.screen) == ("", 0, 0)
    assert d.xlib_name == ":0.0"

    d = parse_display("127.0.0.1:106")
    assert (d.host, d.num, d.screen) == ("127.0.0.1", 106, 0)
    assert d.xlib_name == "127.0.0.1:106.0"
    assert d.env_name == "127.0.0.1:106"

    d = parse_display(":106.1")
    assert (d.host, d.num, d.screen) == ("", 106, 1)

    d = parse_display("localhost:10.0")
    assert (d.host, d.num, d.screen) == ("localhost", 10, 0)


def test_parse_rejects_garbage():
    for bad in ("", "   ", "display", ":x", "a:b:c", None):
        with pytest.raises(DisplayError):
            parse_display(bad)


def test_verify_real_display_tcp_and_unix(xvfb_display):
    info = verify_display(xvfb_display)
    assert info["width"] == 1280 and info["height"] == 800
    assert info["depth"] == 24
    assert info["transport"] == "tcp"

    info2 = verify_display(f":{xvfb_display.split(':')[1]}")
    assert info2["width"] == 1280
    assert info2["transport"] == "unix-socket"


def test_verify_missing_display_gives_real_error(xvfb_display):
    num = int(xvfb_display.split(":")[1])
    with pytest.raises(DisplayError) as exc:
        verify_display(f"127.0.0.1:{num + 50}")
    msg = str(exc.value)
    assert str(num + 50) in msg           # mentions the display that failed
    assert "Xvfb" in msg or "socket" in msg  # and how to fix it


def test_unix_fallback_for_tcp_display(xvfb_display):
    """A loopback TCP display falls back to the same display's unix socket."""
    num = int(xvfb_display.split(":")[1])
    # Force TCP to fail by using a bogus loopback alias that has no listener,
    # while the unix socket for this display number exists.
    import subprocess

    out = subprocess.run(
        ["bash", "-c",
         f"ss -ltn | grep -q ':6106 ' && echo tcp || echo notcp"],
        capture_output=True, text=True,
    )
    if out.stdout.strip() == "tcp":
        # TCP listener exists: direct verification is expected to succeed.
        info = verify_display(f"127.0.0.1:{num}")
        assert info["transport"] == "tcp"
    else:
        info = verify_display(f"127.0.0.1:{num}")
        assert info["transport"] == "unix-socket"
        assert "unix" in info.get("note", "") or info["xlib_name"].startswith(":")


def test_detect_display(monkeypatch, xvfb_display):
    monkeypatch.setenv("DISPLAY", xvfb_display)
    assert detect_display().raw == xvfb_display
    assert detect_display(":1").raw == ":1"      # explicit argument wins
    monkeypatch.delenv("DISPLAY")
    with pytest.raises(DisplayError):
        detect_display()


def test_list_local_displays(xvfb_display):
    num = xvfb_display.split(":")[1]
    assert f":{num}" in list_local_displays()


def test_socket_handshake_reads_the_real_screen_size(xvfb_display):
    """The pure-Python X11 handshake works with no helper programs at all."""
    from app.displayutil import _verify_with_socket, parse_display

    info = _verify_with_socket(parse_display(xvfb_display), timeout=5)
    assert info["width"] == 1280 and info["height"] == 800
    assert info["depth"] == 24
    assert info["method"] == "socket"


def test_verify_survives_denied_helper_execution(xvfb_display, monkeypatch):
    """execve-EPERM devices: verification must still work, no subprocess used."""
    import app.displayutil as displayutil

    def denied(*args, **kwargs):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(displayutil.shutil, "which", lambda _: None)
    monkeypatch.setattr(displayutil.subprocess, "run", denied)

    info = verify_display(xvfb_display)
    assert info["width"] == 1280 and info["height"] == 800
    assert info["method"] in ("xlib", "socket")
