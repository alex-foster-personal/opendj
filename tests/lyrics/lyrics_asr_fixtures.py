"""Shared constants for synthetic lyrics_asr cache fixtures (#11394, refs #5660)."""

from __future__ import annotations

import hashlib
from pathlib import Path

SYNTHETIC_STABLE_ID_PREFIX = "opendj-synthetic-lyrics:"

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lyrics_asr"

CACHE_FIXTURE_NAMES: tuple[str, ...] = (
    "hallucination_arabic_subscribe_fill_cache.json",
    "hallucination_katakana_repeat_cache.json",
    "hallucination_thanks_only_cache.json",
    "invented_arabic_refrain_lrclib_cache.json",
    "invented_chant_syllables_lrclib_cache.json",
    "invented_refrain_variant_lrclib_cache.json",
    "invented_sparse_hook_lrclib_cache.json",
    "invented_sparse_same_script_hook_lrclib_cache.json",
    "invented_trilingual_mixed_lrclib_cache.json",
)

# Captured stable ids from the owner's library (pre-GB-5 filenames) and the
# gb5-invented-fixture: hashes that still collide with library tracks.
REAL_LIBRARY_STABLE_IDS: frozenset[str] = frozenset(
    {
        "f70643404518d4af71a955ac001228896cf30e7f",
        "0c6fcf9a9dde29dd4ac71162e5b57e8024fcae10",
        "66b83341e26c955be715f46bf6c8f9f67722fb33",
        "daacfd89fb24c35f1c09d684b1a7d9aff67676f2",
        "07641ab99b9186184ae3a1df7a240055877d9eb7",
        "277aeba8a97fd3126d41fc544052da0890b48e12",
        "27d881a720290f21a01359dbf52375524a591967",
        "db3697a51a3acdfbf9a4bb254791443abca7dffa",
        "317fa699568b702ef4ea55625aa21b4ecb4cc403",
        "5f837bae3baceca9bcb2ad2402ad8f231eafcd5f",
        "6be9dac1e03272af23cf94ce3c8694dc8e4b1f34",
        "81866cbc29f5b088539aea327c19d03aac812f09",
        "9125cdd80e03030a67482f3eecb3a4fd981d0914",
        "992eda2577dc775bc35e8eea65370e9d04437f92",
        "a60fc4294160596643626c756565b0de1181ccb4",
        "a2ef5f098234381e1acca0c0c5f1bd7a8d2b1a21",
        "c1831c24c993eaf97bf9e80c3cd3a22aec691fb7",
        "f076808222a430574e8801fc2504bac5638839fd",
    }
)

BANNED_CAPTURED_LYRIC_FRAGMENTS: frozenset[str] = frozenset(
    {
        "I'm losing it",
        "From Gaza with love",
        "All I know, all I know, all I know",
        "Pullin', pullin', pullin', pullin'",
        "Came from Gaza with love",
    }
)


def synthetic_stable_id(filename: str) -> str:
    return hashlib.sha1(f"{SYNTHETIC_STABLE_ID_PREFIX}{filename}".encode()).hexdigest()
