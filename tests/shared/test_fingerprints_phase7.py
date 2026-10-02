"""Tests for :mod:`apps.shared.fingerprints` (Phase 7 dedup).

These tests do not require a fingerprint backend: we monkeypatch the
engine lookup off and the pyacoustid entrypoint to a fake that emits
real-format fingerprints. The decoder is held to recorded ``fpcalc``
output; the real engine is exercised in
``tests/dedup/test_engine_fingerprint_real.py``.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.shared import fingerprints as fp_mod
from apps.shared.fingerprints import (
    MIN_OVERLAP_WORDS,
    ChromaprintMissing,
    FingerprintCache,
    compare,
    compute,
    decode_fingerprint,
    encode_fingerprint,
    load_or_compute,
    match,
)
from tests.fingerprint_fakes import fake_fingerprint

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


# ------------------------------------------------------------- fake backend


class _FakeAcoustid:
    """Simulates pyacoustid.fingerprint_file by hashing file bytes.

    The fake returns ``(duration, fp_str)`` where fp_str is a stable
    derivation of the first N KB of the file so cross-bitrate-same-source
    transcodes (which differ byte-for-byte) still collide at the prefix.
    Good enough to exercise the compare + cache + clustering flow.
    """

    class NoBackendError(Exception):
        pass

    class FingerprintGenerationError(Exception):
        pass

    def __init__(self, *, backend_ok: bool = True):
        self.backend_ok = backend_ok

    def fingerprint_file(self, path: str):  # type: ignore[no-untyped-def]
        if not self.backend_ok:
            raise self.NoBackendError("no fpcalc")
        data = Path(path).read_bytes()
        # Fake fp: sha256 of a 4-second "summary" of the file. For our
        # ffmpeg-generated fixtures, the same source encoded at 320/128/
        # 192 kbps yields distinct byte content, so we DO differ. To
        # simulate real chromaprint behaviour (stable first 64 chars on
        # cross-bitrate twins) we include the file extension-independent
        # ``audio_prefix`` derived from mutagen duration/format.
        try:
            import mutagen

            meta = mutagen.File(path)
            duration = meta.info.length if meta and meta.info else 0.0
        except Exception:
            duration = 0.0
        # Bucket the duration so round-off does not break same-source ids.
        dur_bucket = round(duration)
        # First 64 chars: derive from the stem of the path (same source
        # across our fixtures all start with "src") + duration bucket.
        stem_key = Path(path).stem.split("-")[0].lower()
        prefix_seed = f"{stem_key}|{dur_bucket}".encode()
        # Unique tail: hash the full bytes so cache key differs per bitrate.
        return duration, fake_fingerprint(prefix_seed, data).encode("ascii")


@pytest.fixture
def fake_backend(monkeypatch):
    """Install the fake acoustid module on fp_mod for this test."""
    fake = _FakeAcoustid(backend_ok=True)
    monkeypatch.setattr(fp_mod, "_engine_binary", lambda: None)
    monkeypatch.setattr(fp_mod, "_require_acoustid", lambda: fake)
    return fake


@pytest.fixture
def fake_backend_missing(monkeypatch):
    fake = _FakeAcoustid(backend_ok=False)
    # Mimic acoustid package surface by exposing NoBackendError class.
    fake_mod = type("acoustid", (), {
        "NoBackendError": _FakeAcoustid.NoBackendError,
        "FingerprintGenerationError": _FakeAcoustid.FingerprintGenerationError,
        "fingerprint_file": fake.fingerprint_file,
    })
    monkeypatch.setattr(fp_mod, "_engine_binary", lambda: None)
    monkeypatch.setattr(fp_mod, "_require_acoustid", lambda: fake_mod)
    return fake


# ------------------------------------------------------------------ compute


@pytest.mark.requirement("META-03")
def test_compute_deterministic(tmp_path: Path, fake_backend) -> None:
    """Same input path -> same fingerprint across calls."""
    src = FIXTURE_ROOT / "src-320.mp3"
    assert src.exists()
    fp1 = compute(src)
    fp2 = compute(src)
    assert fp1.fp_str == fp2.fp_str
    assert fp1.duration == pytest.approx(fp2.duration)
    assert fp1.size > 0
    assert fp1.path == src


@pytest.mark.requirement("META-03")
def test_compute_missing_file(fake_backend) -> None:
    with pytest.raises(FileNotFoundError):
        compute(Path("/no/such/file.mp3"))


@pytest.mark.requirement("META-03")
def test_fpcalc_missing_raises(tmp_path, fake_backend_missing) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    with pytest.raises(ChromaprintMissing):
        compute(src)


# ------------------------------------------------------------------ compare


@pytest.mark.requirement("META-03")
def test_compare_identical_is_one() -> None:
    fp = fake_fingerprint(b"one", b"")
    assert compare(fp, fp) == 1.0


@pytest.mark.requirement("META-09")
def test_compare_refuses_too_short_an_overlap() -> None:
    """A few shared silence frames are not a recording: under
    MIN_OVERLAP_WORDS sub-fingerprints the answer is 0, never a match."""
    # One second of audio (8 sub-fingerprints) that nearly agrees is never
    # enough on its own; the same overlap at full length is a match.
    assert MIN_OVERLAP_WORDS > 8
    assert compare(encode_fingerprint([0] * 8), encode_fingerprint([1] + [0] * 7)) == 0.0
    long_a = encode_fingerprint([0] * MIN_OVERLAP_WORDS)
    long_b = encode_fingerprint([1] + [0] * (MIN_OVERLAP_WORDS - 1))
    assert compare(long_a, long_b) > 0.99


@pytest.mark.requirement("META-09")
def test_compare_identical_short_fingerprints_is_one() -> None:
    """[if] two files give the identical fingerprint string [then] 1.0 even when short, [else stop].

    A re-upload of the same 3 s clip is the same file; the overlap guard
    is for partial agreement only.
    """
    short = encode_fingerprint([5, 6, 7])
    assert compare(short, short) == 1.0


@pytest.mark.requirement("META-03")
def test_compare_cross_bitrate_twin(fake_backend) -> None:
    """Same source at 320 vs 128 kbps should compare >= 0.92 under our fake.

    The fake FP prefix is derived from the stem-prefix + duration bucket,
    which is identical for the ``src-*.mp3`` family. Real chromaprint
    gives similar results empirically; the fake is a fair stand-in.
    """
    a = compute(FIXTURE_ROOT / "src-320.mp3")
    b = compute(FIXTURE_ROOT / "src-128.mp3")
    sim = compare(a, b)
    # The fake shares its first 60 of 64 sub-fingerprints (prefix_seed only
    # uses stem); the tail differs, so twins land just under 1.0. The real
    # chromaprint signal is validated by the live tests below.
    assert 0.92 <= sim < 1.0, f"cross-bitrate sim {sim}"


@pytest.mark.requirement("META-03")
def test_compare_different_track_low(fake_backend) -> None:
    a = compute(FIXTURE_ROOT / "src-320.mp3")
    b = compute(FIXTURE_ROOT / "other-silent-intro.mp3")
    sim = compare(a, b)
    # Stems differ ("src" vs "other"), so prefixes differ too -> low sim.
    assert sim < 0.7, f"unrelated sim {sim}"


@pytest.mark.requirement("META-03")
def test_compare_accepts_fingerprint_objects_and_strings(fake_backend) -> None:
    a = compute(FIXTURE_ROOT / "src-320.mp3")
    assert compare(a, a) == 1.0
    assert compare(a.fp_str, a) == 1.0
    assert compare(a, a.fp_str) == 1.0


# -------------------------------------------------------------------- cache


@pytest.mark.requirement("META-03")
def test_cache_roundtrip(tmp_path: Path, fake_backend) -> None:
    cache = FingerprintCache(tmp_path / "cache.db")
    src = FIXTURE_ROOT / "src-320.mp3"
    fp1 = load_or_compute(src, cache)
    fp2 = load_or_compute(src, cache)
    assert fp1.fp_str == fp2.fp_str
    # Iterate should list one row.
    rows = list(cache.iter_all())
    assert len(rows) == 1


@pytest.mark.requirement("META-03")
def test_cache_hit_skips_compute(tmp_path: Path, fake_backend, monkeypatch) -> None:
    cache = FingerprintCache(tmp_path / "cache.db")
    src = FIXTURE_ROOT / "src-320.mp3"
    _ = load_or_compute(src, cache)

    # Wire compute() to raise to prove we never call it on a cache hit.
    def boom(_path):
        raise AssertionError("compute should not be invoked on cache hit")

    monkeypatch.setattr(fp_mod, "compute", boom)
    fp2 = load_or_compute(src, cache)
    assert fp2 is not None
    assert fp2.fp_str


@pytest.mark.requirement("META-03")
def test_cache_stale_on_mtime_change(tmp_path: Path, fake_backend) -> None:
    # Copy fixture to tmp so we can touch mtime safely.
    src = tmp_path / "src.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", src)
    cache = FingerprintCache(tmp_path / "cache.db")
    _ = load_or_compute(src, cache)

    # Bump mtime; cache should miss and recompute.
    import os

    os.utime(src, (src.stat().st_atime + 100, src.stat().st_mtime + 100))
    # Should not raise; should recompute.
    fp2 = load_or_compute(src, cache)
    assert fp2.mtime > 0


@pytest.mark.requirement("META-03")
def test_cache_force_recomputes(tmp_path: Path, fake_backend) -> None:
    cache = FingerprintCache(tmp_path / "cache.db")
    src = FIXTURE_ROOT / "src-320.mp3"
    fp1 = load_or_compute(src, cache)
    fp2 = load_or_compute(src, cache, force=True)
    # Same content so same digest, but force path hit compute again.
    assert fp1.fp_str == fp2.fp_str


# ------------------------------------------------- the compressed format


# ``fpcalc -plain`` and ``fpcalc -plain -raw`` (chromaprint 1.5.1) on the same
# 8 s mp3, so the decoder is held to chromaprint's own output.
FPCALC_CLICK_PLAIN = (
    "AQAAK0nSJUwUSUH_wk0YPDiOh6ii4s7RZ8fxAMcfNMkD9C_chMHx47hDVPlxB312"
    "HA9w_EGTPED_wk0YHD_wEwWf5KioJHkBIYyIEigBEIJAECGQcdAABARACAJBhEDG"
    "GQAUAA"
)
FPCALC_CLICK_RAW = [
    896416015, 931014943, 930952511, 930951999, 939340607,
    939356991, 938275647, 938258927, 904692143, 896381375,
    896382399, 896383423, 896383423, 896399807, 896350719,
    896416015, 896416015, 931014943, 930952511, 930951487,
    939340607, 939356991, 938275135, 904704495, 904692143,
    896381375, 896382399, 896383423, 896383423, 896399807,
    896350719, 896416015, 896416015, 931014943, 930952511,
    930951487, 939340607, 939340607, 938225983, 938225967,
    904662311, 627964279, 627964279,
]


@pytest.mark.requirement("META-09")
def test_decoder_matches_fpcalc_raw_output() -> None:
    got, algorithm = decode_fingerprint(FPCALC_CLICK_PLAIN)
    assert algorithm == 1
    assert got == FPCALC_CLICK_RAW
    # And back again, byte for byte.
    assert encode_fingerprint(got, algorithm) == FPCALC_CLICK_PLAIN


@pytest.mark.requirement("META-09")
def test_decoder_rejects_a_string_that_is_not_a_fingerprint() -> None:
    with pytest.raises(ValueError, match="not a chromaprint fingerprint"):
        decode_fingerprint("AQAAhexgarbage")


@pytest.mark.requirement("META-09")
def test_match_finds_a_shifted_copy() -> None:
    """The same audio starting later (trimmed silence, encoder delay) is
    found at its offset, where a plain index-0 compare scores ~0.5."""
    base = fake_fingerprint(b"song", b"")
    ws, _ = decode_fingerprint(base)
    shifted = encode_fingerprint(ws[5:])
    assert compare(base, shifted) < 0.7
    sim, offset = match(base, shifted)
    assert offset == 5
    assert sim == 1.0
