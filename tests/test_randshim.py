"""Entropy fallbacks: session start must survive a denied kernel entropy source.

Some customer devices raise ``OSError: [Errno 1] Operation not permitted`` from
``os.urandom`` / ``getrandom`` (restricted root consoles, sandboxed
appliances).  WebRTC needs random numbers at session start, so the fallback
must work before anything else happens.
"""

from __future__ import annotations

import os
import random

from app import randshim


def test_urandom_falls_back_when_kernel_denies(monkeypatch):
    def denied(n):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(randshim, "_real_urandom", denied)
    data = os.urandom(32)
    assert len(data) == 32
    assert randshim.used_fallback is True


def test_random_module_survives_getrandbits_denial(monkeypatch):
    def denied(self, k):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(randshim, "_real_getrandbits", denied)

    value = random.randint(0, 0xFFFF)  # aiortc SCTP local port
    assert 0 <= value <= 0xFFFF
    assert 0 <= random.getrandbits(16) < 1 << 16
    assert random.choice("abcdefghij") in "abcdefghij"
    assert random.uniform(0.8, 1.2) >= 0.8  # aioice consent jitter
    assert isinstance(os.urandom(16), bytes)  # aiortc HMAC key


def test_getrandom_shim_if_present(monkeypatch):
    if getattr(os, "getrandom", None) is not randshim._getrandom:
        return  # shim not installed for getrandom on this platform

    def denied(n, flags=0):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(randshim, "_real_getrandom", denied)
    assert len(os.getrandom(8)) == 8


def test_install_is_idempotent():
    before = os.urandom
    randshim.install()
    assert os.urandom is before
