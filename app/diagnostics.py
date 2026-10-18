"""Session-failure diagnostics: say exactly what failed, where, and why.

Every report answers, unambiguously:

* which build was running (version, package path, launcher),
* which step of session start was in progress when it died,
* the exact exception (class, errno, strerror) with the full traceback,
* a one-by-one probe of every capability session start needs - the kernel
  entropy source, helper-program execution, sockets, the X11 session - each
  marked OK / FAIL / INFO,
* a plain-English reading of what the probes mean, so the report cannot be
  mistaken for a different problem.

Reports are written to ``~/.local/share/tech-support/last-error.txt`` and
shown in the customer app's error screen.
"""

from __future__ import annotations

import asyncio
import errno as _errno
import os
import platform
import random
import shutil
import socket
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field

from .version import APP_VERSION

OK = "OK"
FAIL = "FAIL"
INFO = "INFO"
SKIP = "SKIP"


@dataclass
class Probe:
    status: str
    label: str
    detail: str


@dataclass
class StageTrace:
    """Records the steps of session start so a failure names its exact step."""

    steps: list = field(default_factory=list)
    _open: list = field(default_factory=list)

    @contextmanager
    def stage(self, name: str, what: str = ""):
        entry = {"name": name, "what": what, "start": time.time(), "end": None, "error": None}
        self.steps.append(entry)
        self._open.append(entry)
        try:
            yield entry
        except BaseException as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            entry["end"] = time.time()
            self._open.pop()

    @property
    def current(self) -> str:
        return self._open[-1]["name"] if self._open else ""


# --------------------------------------------------------------------- probes


def _probe(label: str, fn, info: bool = False) -> Probe:
    try:
        return Probe(INFO if info else OK, label, str(fn()))
    except Exception as exc:
        return Probe(FAIL, label, f"{type(exc).__name__}: {exc}")


def _errno_text(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.errno is not None:
        name = _errno.errorcode.get(exc.errno, "?")
        return f"{exc} [{name}]"
    return f"{type(exc).__name__}: {exc}"


def candidates_from_sdp(sdp: str) -> list[str]:
    """Return the 'a=candidate:' lines of an SDP as readable strings."""
    out: list[str] = []
    for line in (sdp or "").splitlines():
        line = line.strip()
        if line.startswith("a=candidate:"):
            out.append(line[len("a="):])
    return out


def _turn_entry(ice_servers) -> dict | None:
    """Find the first TURN relay entry in a [{urls,username,credential}] list."""
    for item in ice_servers or []:
        urls = item.get("urls") if isinstance(item, dict) else item
        if isinstance(urls, str):
            urls = [urls]
        for url in urls or []:
            if isinstance(url, str) and url.startswith(("turn:", "turns:")):
                entry = {"urls": url}
                if isinstance(item, dict):
                    entry["username"] = item.get("username")
                    entry["credential"] = item.get("credential")
                return entry
    return None


def _probe_turn_allocation(ice_servers) -> str:
    """Allocate a relay on the configured TURN server.  Never raises."""
    entry = _turn_entry(ice_servers)
    if entry is None:
        return "no TURN relay configured (direct peer-to-peer only)"
    url = entry["urls"]
    scheme, rest = url.split(":", 1)
    rest = rest.split("?", 1)[0]
    host, _, port_s = rest.rpartition(":")
    if not host:
        host, port_s = rest, ""
    port = int(port_s) if port_s.isdigit() else (5349 if scheme == "turns" else 3478)
    transport = "tcp" if scheme == "turns" else "udp"
    if "transport=tcp" in url:
        transport = "tcp"
    username = entry.get("username")
    credential = entry.get("credential")
    if not username:
        return f"{url}: anonymous allocation not attempted (no credentials)"

    class _Sink(asyncio.DatagramProtocol):
        def connection_made(self, transport_):  # noqa: ANN001
            self.transport = transport_

    async def _run():
        from aioice import turn as aioice_turn

        endpoint, _ = await asyncio.wait_for(
            aioice_turn.create_turn_endpoint(
                lambda: _Sink(),
                (host, port),
                username=username,
                password=credential,
                ssl=(scheme == "turns") or None,
                transport=transport,
            ),
            timeout=10,
        )
        relay = endpoint.transport.get_extra_info("related_address")
        endpoint.transport.close()
        return relay

    import concurrent.futures

    try:
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            relay = pool.submit(lambda: asyncio.run(_run())).result(timeout=15)
    except Exception as exc:
        raise RuntimeError(
            f"{url} as user {username!r}: allocation failed "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    return f"{url} -> relay address {relay} (server reachable, credentials valid)"


def probe_environment(ice_servers=None) -> list[Probe]:
    """Probe everything session start depends on.  Never raises."""
    probes: list[Probe] = []

    def add(label: str, fn, info: bool = False) -> None:
        probes.append(_probe(label, fn, info=info))

    import app
    from app import randshim

    # ---- identity: which build, launched how
    add("app.version", lambda: APP_VERSION, info=True)
    add("app.package_path", lambda: os.path.dirname(os.path.abspath(app.__file__)), info=True)
    add("app.launcher", lambda: sys.argv[0], info=True)
    add(
        "python",
        lambda: f"{platform.python_version()} ({platform.python_implementation()}) "
        f"at {sys.executable}",
        info=True,
    )
    add("platform", lambda: platform.platform(), info=True)
    add("cwd", lambda: os.getcwd(), info=True)

    # ---- who we are (root is fine - this is just recorded, never assumed)
    add(
        "user.ids",
        lambda: f"uid={os.getuid()} euid={os.geteuid()} gid={os.getgid()} "
        f"({'root' if os.geteuid() == 0 else 'not root'})",
        info=True,
    )

    def _status_field(name: str) -> str:
        with open("/proc/self/status", "r", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith(name + ":"):
                    return line.split(":", 1)[1].strip()
        return "unknown"

    add("kernel.seccomp", lambda: _status_field("Seccomp") + " (0=off 1=strict 2=filter)", info=True)
    add("kernel.caps", lambda: "CapEff=" + _status_field("CapEff"), info=True)
    add(
        "security.module",
        lambda: open("/proc/self/attr/current", encoding="ascii", errors="replace").read().strip(),
        info=True,
    )

    # ---- entropy: the source that killed sessions on locked-down devices.
    # Probe the KERNEL sources directly (the shim may mask them).
    add("entropy.kernel_urandom", lambda: randshim._real_urandom(8).hex(), info=True)

    def _getrandom() -> str:
        if randshim._real_getrandom is None:
            raise RuntimeError("not available on this platform")
        return randshim._real_getrandom(8).hex()

    if randshim._real_getrandom is not None:
        add("entropy.kernel_getrandom", _getrandom, info=True)
    else:
        probes.append(Probe(SKIP, "entropy.kernel_getrandom", "no os.getrandom on this platform"))

    def _dev_urandom() -> str:
        with open("/dev/urandom", "rb") as fh:
            return fh.read(8).hex()

    add("entropy.dev_urandom", _dev_urandom, info=True)

    def _dev_stat() -> str:
        st = os.stat("/dev/urandom")
        return f"mode={oct(st.st_mode & 0o777)} uid={st.st_uid}"

    add("entropy.dev_urandom_mode", _dev_stat, info=True)
    add("entropy.random_getrandbits", lambda: hex(random.getrandbits(32)), info=True)
    add(
        "entropy.shim",
        lambda: f"installed={getattr(os.urandom, '_tech_support_shim', False)} "
        f"fallback_used={randshim.used_fallback}",
        info=True,
    )

    # ---- helper execution (execve denials show up as EPERM)
    add(
        "exec.python",
        lambda: _spawn_ok([sys.executable, "-c", "pass"]) or "spawned",
    )
    add("exec.bin_true", lambda: _spawn_ok(["true"]) or "spawned")

    # ---- sockets (WebRTC needs UDP; the UI needs the X11 unix socket)
    def _udp() -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("0.0.0.0", 0))
            return f"bound udp {s.getsockname()}"
        finally:
            s.close()

    add("net.socket_udp", _udp)
    add("net.socket_unix", lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close() or "created")

    def _netlink() -> str:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 0)  # NETLINK_ROUTE
        sock.close()
        return "created"

    add("net.netlink_socket", _netlink)

    def _ifaddr() -> str:
        import ifaddr

        return f"{len(list(ifaddr.get_adapters()))} adapters"

    add("net.ifaddr_enumeration", _ifaddr)

    def _host_addresses() -> str:
        from .iceshim import _get_host_addresses

        return ", ".join(_get_host_addresses(True, True)) or "(none)"

    add("net.ice_host_addresses", _host_addresses)

    # ---- the graphical session
    add("x11.display", lambda: os.environ.get("DISPLAY", "(unset)"), info=True)
    add("x11.session_type", lambda: os.environ.get("XDG_SESSION_TYPE", "(unset)"), info=True)
    add("x11.wayland_display", lambda: os.environ.get("WAYLAND_DISPLAY", "(unset)"), info=True)

    def _auth() -> str:
        path = os.environ.get("XAUTHORITY") or os.path.expanduser("~/.Xauthority")
        if not os.path.exists(path):
            return f"{path}: absent"
        return f"{path}: exists mode={oct(os.stat(path).st_mode & 0o777)}"

    add("x11.authority_file", _auth, info=True)
    add("x11.xdpyinfo_binary", lambda: shutil.which("xdpyinfo") or "(not installed)", info=True)

    def _x11_verify() -> str:
        from .displayutil import verify_display

        disp = os.environ.get("DISPLAY", "")
        if not disp:
            raise RuntimeError("DISPLAY is not set")
        info = verify_display(disp, timeout=5)
        return f"{disp} -> {info['width']}x{info['height']} via {info.get('method', '?')}"

    add("x11.verify_display", _x11_verify)

    # ---- the TURN relay (only relevant once a session is trying to connect)
    if ice_servers is not None:
        add("net.turn_relay", lambda: _probe_turn_allocation(ice_servers))

    return probes


def _spawn_ok(cmd: list) -> str:
    try:
        subprocess.run(cmd, capture_output=True, timeout=5, check=True)
        return ""
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"exit {exc.returncode}") from exc


# ------------------------------------------------------------------- reports


def _interpret(probes: list[Probe], exc: BaseException | None) -> list[str]:
    notes: list[str] = []
    by_label = {p.label: p for p in probes}

    def failed(label: str) -> bool:
        p = by_label.get(label)
        return p is not None and p.status == FAIL

    turn = by_label.get("net.turn_relay")
    if isinstance(exc, TimeoutError):
        notes.append(
            "This is NOT a startup failure: every step above completed. The "
            "peer-to-peer connection itself never came up. Read CONNECTION "
            "STATE above: if the technician's answer was NEVER RECEIVED, the "
            "signaling link dropped it; if both sides list candidates but no "
            "pair connected, this network blocks peer-to-peer traffic (UDP or "
            "all inbound) and a reachable TURN relay is required."
        )
    if turn is not None and turn.status == FAIL:
        notes.append(
            "The configured TURN relay could not allocate a relay address "
            "(net.turn_relay probe line has the exact reason). Without a "
            "working relay only direct peer-to-peer paths can connect, and "
            "this network appears to block them. The relay is either "
            "unreachable from here or rejected the credentials."
        )
    if turn is not None and turn.status == OK:
        notes.append(
            "The TURN relay IS reachable from this device, so the failure is "
            "on the technician side or between the two: see which candidates "
            "each side held in CONNECTION STATE above."
        )

    if failed("entropy.kernel_urandom") or failed("entropy.kernel_getrandom"):
        notes.append(
            "The operating system DENIES the kernel random-number source "
            "(getrandom/urandom -> EPERM). This is common in restricted root "
            "or sandboxed sessions and is NOT a file-permissions problem. "
            "The app falls back automatically; session start no longer needs it."
        )
    if failed("entropy.dev_urandom"):
        notes.append(
            "Even /dev/urandom cannot be read, so the app uses its built-in "
            "fallback stream. Session start no longer needs the OS entropy source."
        )
    if failed("exec.python") or failed("exec.bin_true"):
        notes.append(
            "The operating system REFUSES to start helper programs (execve -> "
            "EPERM). The app avoids helpers where possible; screen capture falls "
            "back to in-process X11 capture."
        )
    if failed("net.socket_udp"):
        notes.append(
            "UDP sockets cannot be created, so the WebRTC media connection is "
            "impossible on this device in its current sandbox/permissions."
        )
    if failed("net.netlink_socket") or failed("net.ifaddr_enumeration"):
        notes.append(
            "The OS blocks network-interface enumeration (netlink/getifaddrs "
            "-> EPERM). This is the sandbox denial that stopped candidate "
            "gathering. The app now falls back to address sources that need "
            "no netlink; if gathering still fails, the sandbox must allow "
            "netlink or at least outbound UDP sockets."
        )
    if failed("x11.verify_display"):
        notes.append(
            "The app cannot reach the graphical session it should share "
            "(X11 verify failed - see the probe line for the exact reason)."
        )
    if isinstance(exc, OSError) and exc.errno == 1 and not notes:
        notes.append(
            "The OS refused an operation with EPERM and every measured "
            "capability works, so the refusal happened inside the step above. "
            "The traceback names the exact call - send this whole report."
        )
    if not notes:
        notes.append(
            "Everything measurable works. The failure is in the step named "
            "above; its traceback is the exact source. Send this whole report."
        )
    return notes


def build_report(
    exc: BaseException,
    trace: StageTrace | None = None,
    context: str = "",
    details: str = "",
    ice_servers=None,
) -> str:
    """Build the complete, self-contained failure report."""
    probes = probe_environment(ice_servers=ice_servers)
    lines: list[str] = []
    lines.append("=" * 66)
    lines.append("TECH SUPPORT - SESSION START FAILURE REPORT")
    lines.append("=" * 66)
    lines.append(f"time          : {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
    lines.append(f"app version   : {APP_VERSION}")
    if context:
        lines.append(f"context       : {context}")

    if trace is not None and trace.steps:
        lines.append("")
        lines.append("STEP TIMELINE (session start)")
        for i, s in enumerate(trace.steps, 1):
            dur = "-" if s["end"] is None else f"{(s['end'] - s['start']) * 1000:.0f}ms"
            mark = "x" if s["error"] else ("." if s["end"] is not None else ">")
            lines.append(f"  [{mark}] {i:02d}. {s['name']} ({dur})")
            if s["what"]:
                lines.append(f"        does: {s['what']}")
            if s["error"]:
                lines.append(f"        ERROR: {s['error']}")
        failed_steps = [s for s in trace.steps if s["error"]]
        if failed_steps:
            s = failed_steps[-1]
            lines.append("")
            lines.append(f"FAILING STEP   : {s['name']}")
            if s["what"]:
                lines.append(f"WHAT IT DOES   : {s['what']}")

    lines.append("")
    lines.append(f"ERROR          : {_errno_text(exc)}")
    if isinstance(exc, OSError) and exc.errno is not None:
        lines.append(
            f"ERRNO          : {exc.errno} ({_errno.errorcode.get(exc.errno, '?')}) "
            f"- {os.strerror(exc.errno)}"
        )

    if details:
        lines.append("")
        lines.append("CONNECTION STATE (measured just before giving up)")
        for line in details.splitlines():
            lines.append(f"  {line}" if line.strip() else "")

    lines.append("")
    lines.append("TRACEBACK")
    for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
        for sub in line.rstrip("\n").splitlines() or [""]:
            lines.append(f"  {sub}")

    lines.append("")
    lines.append("CAPABILITY PROBES (each thing session start needs, checked one by one)")
    width = max(len(p.label) for p in probes)
    for p in probes:
        lines.append(f"  [{p.status:<4}] {p.label:<{width}} : {p.detail}")

    lines.append("")
    lines.append("WHAT THIS MEANS")
    for note in _interpret(probes, exc):
        for i, chunk in enumerate(_wrap(note, 62)):
            lines.append(f"  {'- ' if i == 0 else '  '}{chunk}")

    lines.append("=" * 66)
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    out: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out or [""]


def save_report(report: str) -> str:
    """Write the report where it can be read out later; returns its path."""
    directory = os.path.expanduser("~/.local/share/tech-support")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        directory = "/tmp"
    path = os.path.join(directory, "last-error.txt")
    stamp = os.path.join(directory, f"error-{time.strftime('%Y%m%d-%H%M%S')}.txt")
    for target in (path, stamp):
        try:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(report + "\n")
        except OSError:
            continue
    return path
