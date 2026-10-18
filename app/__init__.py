"""Customer-side application package."""

# Entropy fallbacks first: some customer devices deny getrandom()/urandom and
# WebRTC needs random numbers at session start.  See app/randshim.py.
from . import randshim  # noqa: F401

randshim.install()
