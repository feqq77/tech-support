"""ICE address discovery must survive devices that deny netlink/getifaddrs."""

from __future__ import annotations

import socket

from app import iceshim


def test_fallback_when_enumeration_raises():
    def denied(use_ipv4, use_ipv6):
        raise OSError(1, "Operation not permitted")

    original = iceshim._original
    iceshim._original = denied
    try:
        addresses = iceshim._get_host_addresses(True, True)
    finally:
        iceshim._original = original

    assert addresses, "fallback must find at least one address"
    for addr in addresses:
        assert addr not in ("", "0.0.0.0")


def test_fallback_when_enumeration_returns_nothing():
    def empty(use_ipv4, use_ipv6):
        return []

    original = iceshim._original
    iceshim._original = empty
    try:
        addresses = iceshim._get_host_addresses(True, False)
    finally:
        iceshim._original = original

    assert addresses
    assert all(":" not in a for a in addresses)  # IPv4-only request


def test_loopback_is_the_last_resort(monkeypatch):
    def denied(use_ipv4, use_ipv6):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(iceshim, "_original", denied)
    monkeypatch.setattr(iceshim, "_via_hostname", lambda a, b: [])
    monkeypatch.setattr(iceshim, "_via_connected_socket", lambda a, b: [])
    assert iceshim._get_host_addresses(True, True) == ["127.0.0.1", "::1"]


def test_connected_socket_finds_a_real_address():
    addresses = iceshim._via_connected_socket(True, False)
    assert addresses
    socket.inet_aton(addresses[0])  # a real IPv4 address


def test_install_patches_aioice():
    from aioice import ice

    iceshim.install()
    assert ice.get_host_addresses is iceshim._get_host_addresses
    iceshim.install()  # idempotent
    assert ice.get_host_addresses is iceshim._get_host_addresses
