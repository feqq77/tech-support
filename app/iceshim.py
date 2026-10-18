"""Local-address discovery that works where netlink is denied.

aioice gathers ICE candidates through ``ifaddr.get_adapters()``, which calls
glibc ``getifaddrs()`` - and that opens a NETLINK_ROUTE socket.  Restricted
root consoles and sandboxed appliances commonly deny netlink with
``EPERM: Operation not permitted``, which killed candidate gathering
("gather connection candidates") before a single network candidate existed.

This module keeps the normal enumeration and falls back to sources that need
no netlink at all:

1. ``socket.getaddrinfo(hostname)`` - name resolution, no netlink;
2. a connected UDP socket's local address (no packet is ever sent);
3. loopback, as a last resort - a local-only session still works.
"""

from __future__ import annotations

import ipaddress
import socket

_installed = False
_original = None


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def _via_hostname(use_ipv4: bool, use_ipv6: bool) -> list[str]:
    out: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_DGRAM)
    except OSError:
        return out
    for family, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        if family == socket.AF_INET and use_ipv4:
            out.append(addr)
        elif family == socket.AF_INET6 and use_ipv6:
            out.append(addr)
    return out


def _via_connected_socket(use_ipv4: bool, use_ipv6: bool) -> list[str]:
    """Ask the routing stack which source address it would use.  No netlink."""
    out: list[str] = []
    targets = []
    if use_ipv4:
        targets.append((socket.AF_INET, ("8.8.8.8", 53)))
    if use_ipv6:
        targets.append((socket.AF_INET6, ("2001:4860:4860::8888", 53)))
    for family, target in targets:
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM)
        except OSError:
            continue
        try:
            sock.connect(target)  # connect() on UDP sends no packets
            addr = sock.getsockname()[0]
            if addr and not addr.startswith("127.") and addr != "::1":
                out.append(addr)
        except OSError:
            continue
        finally:
            try:
                sock.close()
            except OSError:
                pass
    return out


def _loopback(use_ipv4: bool, use_ipv6: bool) -> list[str]:
    out = []
    if use_ipv4:
        out.append("127.0.0.1")
    if use_ipv6:
        out.append("::1")
    return out


def _get_host_addresses(use_ipv4: bool, use_ipv6: bool) -> list[str]:
    addresses: list[str] = []
    try:
        addresses = _original(use_ipv4, use_ipv6)  # normal path: getifaddrs
    except Exception:
        addresses = []
    if addresses:
        return addresses

    def collect(pool: list[str]) -> list[str]:
        out: list[str] = []
        for addr in pool:
            if addr not in out:
                out.append(addr)
        return out

    # Prefer routable addresses: the connected-socket source answers with the
    # address the routing table actually uses, which is the one a technician's
    # browser can reach.  Hostname answers often include loopback-mapped names
    # (127.0.1.1 on Debian/Ubuntu), which work only for same-machine sessions.
    routable: list[str] = []
    fallback: list[str] = []
    for source in (_via_connected_socket, _via_hostname):
        try:
            found = source(use_ipv4, use_ipv6)
        except Exception:
            found = []
        for addr in collect(found):
            if _is_loopback(addr):
                if addr not in fallback:
                    fallback.append(addr)
            elif addr not in routable:
                routable.append(addr)
    return routable or fallback or _loopback(use_ipv4, use_ipv6)


def install() -> None:
    """Replace aioice's interface enumeration with the resilient one."""
    global _installed, _original
    if _installed:
        return
    try:
        from aioice import ice as _ice
    except Exception:
        return
    _original = _ice.get_host_addresses
    _ice.get_host_addresses = _get_host_addresses
    _installed = True
