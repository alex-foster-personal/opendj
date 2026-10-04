"""Deterministic stand-in fingerprints for the dedup tests.

A fake fingerprint is a REAL chromaprint string (``encode_fingerprint``), so
it travels the same decode and compare path a real one does. Its first
``SHARED`` sub-fingerprints depend only on a seed the caller derives from
the audio's identity, and the rest on the file's bytes: two encodings of the
same source score ~0.97 against each other, unrelated audio ~0.5.
"""
from __future__ import annotations

import hashlib

from apps.shared.fingerprints import encode_fingerprint

SHARED = 60
TOTAL = 64


def _words(seed: bytes, n: int) -> list[int]:
    out: list[int] = []
    counter = 0
    while len(out) < n:
        digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        out.extend(int.from_bytes(digest[k : k + 4], "big") for k in range(0, 32, 4))
        counter += 1
    return out[:n]


def fake_fingerprint(identity_seed: bytes, file_bytes: bytes) -> str:
    """A chromaprint string shared in its first SHARED words by every file
    with the same ``identity_seed``."""
    words = _words(b"id|" + identity_seed, SHARED) + _words(
        b"file|" + hashlib.sha256(file_bytes).digest(), TOTAL - SHARED
    )
    return encode_fingerprint(words)
