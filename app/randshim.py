"""Entropy that never fails - installed before anything needs a random number.

Some customer devices run in locked-down sessions (restricted root consoles,
sandboxed appliances) where the kernel entropy source is denied:  ``os.urandom``
and ``os.getrandom`` raise ``OSError: [Errno 1] Operation not permitted``.
WebRTC needs random numbers the moment a session's data channel is created
(SCTP port, HMAC key, candidate foundations), so that single denial killed
every session start with an error that looked like a permissions problem.

This module keeps the normal fast paths - the real ``os.urandom`` /
``os.getrandom`` / ``random`` - and only falls back when they raise:

1. read ``/dev/urandom`` (or ``/dev/random``) directly, which usually works
   even where ``getrandom(2)`` is filtered;
2. as a last resort, a hashed stream mixed from the time, the pid and a
   counter.  The numbers are used for port numbers, foundation strings and
   session HMAC keys - not for anything security-critical on its own.

Imported from ``app/__init__`` so it is in place before aiortc / aioice bind
their own references to ``os.urandom`` and ``random``.
"""

from __future__ import annotations

import hashlib
import os
import random as _random
import time

#: set when the fallback path was used at least once (surfaced in the log)
used_fallback = False

_real_urandom = os.urandom
_real_getrandom = getattr(os, "getrandom", None)
_real_getrandbits = _random.Random.getrandbits

_counter = 0
_last = 0


def _fallback_bytes(n: int) -> bytes:
    global used_fallback, _counter, _last
    used_fallback = True
    for path in ("/dev/urandom", "/dev/random"):
        try:
            with open(path, "rb") as fh:
                data = fh.read(n)
            if len(data) == n:
                return data
        except Exception:
            continue
    out = bytearray()
    while len(out) < n:
        _counter += 1
        block = hashlib.sha256(
            f"{time.time_ns()}:{os.getpid()}:{_counter}:{_last}".encode()
        ).digest()
        _last = int.from_bytes(block[:8], "big")
        out += block
    return bytes(out[:n])


def _urandom(n: int) -> bytes:
    try:
        return _real_urandom(n)
    except Exception:
        return _fallback_bytes(n)


def _getrandom(n: int, flags: int = 0) -> bytes:
    try:
        return _real_getrandom(n, flags)  # type: ignore[misc]
    except Exception:
        return _fallback_bytes(n)


def _safe_getrandbits(self, k: int) -> int:
    try:
        return _real_getrandbits(self, k)
    except Exception:
        used = _fallback_bytes((k + 7) // 8 or 1)
        return int.from_bytes(used, "big") & ((1 << k) - 1)


def install() -> None:
    """Idempotently install the fallbacks."""
    if getattr(os.urandom, "_tech_support_shim", False):
        return
    os.urandom = _urandom  # type: ignore[assignment]
    _urandom._tech_support_shim = True  # type: ignore[attr-defined]
    if _real_getrandom is not None:
        os.getrandom = _getrandom  # type: ignore[assignment]
    _random.Random.getrandbits = _safe_getrandbits  # type: ignore[assignment]
    # (SystemRandom.getrandbits goes through random._urandom, rebound below.)
    # random.py captures os.urandom into its own _urandom name at import time.
    _random._urandom = _urandom  # type: ignore[attr-defined]
    try:
        _random.seed()
    except Exception:
        pass
