"""Shared constants for synthetic lyrics_asr cache fixtures (#11394, refs #5660)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.fixture_real_content_rules import (
    BANNED_CAPTURED_LYRIC_FRAGMENTS,
    REAL_LIBRARY_STABLE_IDS,
)

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


def synthetic_stable_id(filename: str) -> str:
    return hashlib.sha1(f"{SYNTHETIC_STABLE_ID_PREFIX}{filename}".encode()).hexdigest()
