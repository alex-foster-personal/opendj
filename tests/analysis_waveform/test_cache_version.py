"""A pre-tri-band cache entry is REBUILT, never reshaped (NATIVE-06).

The failure this guards is silent and looks fine on screen: the mono cache
holds a 1-D uint8 array, the tri-band reader wants ``(n, 3)``, and
``reshape(-1, 3)`` on a mono array succeeds whenever its length divides by
three - producing a waveform drawn from real bytes read under the wrong
convention. Nothing downstream can tell that apart from a correct decode, so
the guard has to be the cache key itself.

  - [if] a mono cache entry is served to the tri-band reader [then] fail, [else stop].
  - [if] a crossover change leaves old peaks reading as current [then] fail, [else stop].
  - [if] a current entry written by this decoder is NOT read back [then] fail, [else stop].
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.analysis_waveform import decode, local_waveform
from apps.analysis_waveform.lane_payload import build_waveform_payload

pytestmark = pytest.mark.requirement("NATIVE-06")

SID = "a" * 40


@pytest.fixture
def source(tmp_path: Path) -> Iterator[Path]:
    """A disposable cache directory and a real file to key entries against.

    ``LOCAL_WAVEFORM_CACHE_DIR`` is redirected by assignment and restored in a
    ``finally``, which is the mechanism ``apps/adapters/rekordbox/config.py``
    documents as its own supported override ("Every constant here is a
    rebindable module attribute, on purpose"), not a patched production
    function. No behavior under test is replaced: the cache writer, the reader
    and the version derivation are all the shipped ones.
    """
    original = rb_config.LOCAL_WAVEFORM_CACHE_DIR
    rb_config.LOCAL_WAVEFORM_CACHE_DIR = tmp_path / "cache"
    try:
        path = tmp_path / "source.wav"
        path.write_bytes(b"RIFF" + b"\x00" * 4096)
        yield path
    finally:
        rb_config.LOCAL_WAVEFORM_CACHE_DIR = original


def _tri_peaks(columns: int = 600) -> np.ndarray:
    """Three DISTINGUISHABLE bands, so a column-order mistake cannot round-trip."""
    return np.stack(
        [
            np.arange(columns, dtype=np.uint8),
            np.full(columns, 7, dtype=np.uint8),
            np.full(columns, 200, dtype=np.uint8),
        ],
        axis=1,
    )


def test_a_current_entry_round_trips_exactly(source: Path) -> None:
    """The positive control: without this, every assertion below could pass
    because the cache never reads anything back at all."""
    key = local_waveform._decode_key(source)
    peaks = _tri_peaks()
    local_waveform._store_peaks(SID, key, peaks)

    read_back = local_waveform._cached_peaks(SID, key)
    assert read_back is not None, "a just-written current entry must be a cache HIT"
    assert np.array_equal(read_back, peaks)
    assert local_waveform.local_preview_strip(SID)[0] is not None


def test_old_pwv6_music_one_strip_sidecar_is_re_derived_not_served(
    source: Path,
) -> None:
    """[if] old cached peaks reshape into new strip scale without re-decode [then] fail, [else stop]."""
    from apps.analysis_waveform.bands import PWV6_SCALE_VERSION

    key = local_waveform._decode_key(source)
    local_waveform._store_peaks(SID, key, _tri_peaks())
    strip_path = local_waveform._strip_path(SID)
    entry = json.loads(strip_path.read_text(encoding="utf-8"))
    entry["strip_scale"] = "pwv6-music-1"
    local_waveform._write_json(strip_path, entry)
    b64, peak_max = local_waveform.local_preview_strip(SID)
    assert b64 is not None and peak_max is not None
    rewritten = json.loads(strip_path.read_text(encoding="utf-8"))
    assert rewritten["strip_scale"] == PWV6_SCALE_VERSION


def test_a_tri_one_headroom_entry_is_a_cache_miss(source: Path) -> None:
    """[if] old s16 peaks served under tri-2 headroom [then] fail, [else stop]."""
    key = local_waveform._decode_key(source)
    peaks = _tri_peaks()
    legacy = ":".join(
        str(getattr(decode.PROFILE, f.name))
        for f in fields(decode.PROFILE)
        if f.name != "peak_headroom_db"
    )
    old_version = f"tri-1:{legacy}"
    assert old_version != local_waveform.peaks_version()
    local_waveform._write_json(
        local_waveform._entry_path(SID),
        {
            "schema": rb_config.LOCAL_WAVEFORM_CACHE_SCHEMA,
            "peaks_version": old_version,
            "peaks_b64": base64.b64encode(peaks.tobytes()).decode("ascii"),
            **key,
        },
    )
    assert local_waveform._cached_peaks(SID, key) is None


def test_a_pre_tri_band_mono_entry_is_a_cache_miss(source: Path) -> None:
    key = local_waveform._decode_key(source)
    mono = np.arange(600, dtype=np.uint8)
    assert mono.size % decode.BAND_COUNT == 0, (
        "the fixture must be reshapeable to (n, 3), or this test proves nothing: "
        "a length that does not divide by 3 would be rejected by arithmetic "
        "rather than by the version key under test"
    )
    # The entry a pre-NATIVE-06 build wrote: schema 1, samples_per_column, no
    # peaks_version, 1-D peaks.
    local_waveform._write_json(
        local_waveform._entry_path(SID),
        {
            "schema": 1,
            "samples_per_column": 53,
            "peaks_b64": base64.b64encode(mono.tobytes()).decode("ascii"),
            **key,
        },
    )
    assert local_waveform._cached_peaks(SID, key) is None, (
        "a mono entry must be rebuilt, never reshaped into three invented bands"
    )


def test_peaks_written_under_another_producer_profile_are_a_cache_miss(
    source: Path,
) -> None:
    """A real producer-configuration transition, with nothing patched.

    ``DecodeProfile`` is a value, so a DIFFERENT real producer configuration is
    constructed rather than simulated: the entry below is written with the
    version the shipped code produces for a 5 kHz high crossover, and the
    shipped reader - running on the shipped profile - must miss it. An earlier
    draft mutated ``decode.CROSSOVER_HIGH_HZ`` on an imported module, which is
    monkeypatching a production path and is forbidden outright by AGENTS.md
    "No mocks and locked real fixtures" (Codex review, PR #1536).
    """
    other = decode.DecodeProfile(crossover_high_hz=5_000)
    assert local_waveform.peaks_version(other) != local_waveform.peaks_version(), (
        "the fixture profile must actually differ from the shipped one"
    )
    key = local_waveform._decode_key(source)
    peaks = _tri_peaks()
    local_waveform._write_json(
        local_waveform._entry_path(SID),
        {
            "schema": rb_config.LOCAL_WAVEFORM_CACHE_SCHEMA,
            "peaks_version": local_waveform.peaks_version(other),
            "peaks_b64": base64.b64encode(peaks.tobytes()).decode("ascii"),
            **key,
        },
    )
    assert local_waveform._cached_peaks(SID, key) is None, (
        "peaks measured under another crossover must be rebuilt, not served"
    )


def test_the_strip_sidecar_carries_the_same_version_as_the_entry(source: Path) -> None:
    """The half a cache-key change is easiest to forget: the browser strip is a
    separate file and would otherwise outlive the peaks it came from."""
    key = local_waveform._decode_key(source)
    local_waveform._store_peaks(SID, key, _tri_peaks())
    assert local_waveform.local_preview_strip(SID)[0] is not None, "precondition: a hit"

    strip = json.loads(local_waveform._strip_path(SID).read_text(encoding="utf-8"))
    assert strip["peaks_version"] == local_waveform.peaks_version()
    strip["peaks_version"] = local_waveform.peaks_version(
        decode.DecodeProfile(crossover_high_hz=5_000)
    )
    local_waveform._write_json(local_waveform._strip_path(SID), strip)
    assert local_waveform.local_preview_strip(SID) == (None, None)


def test_the_version_moves_for_every_field_that_defines_the_peaks() -> None:
    """One real profile per field, each of which must move the version.

    The transition test above only exercises the high crossover. A derivation
    that interpolated that ONE field and hard-coded the rest would pass it while
    leaving a rate or filter-order change silently readable from cache. Every
    field is walked from the dataclass itself, so a field ADDED to
    ``DecodeProfile`` later is covered without anyone editing this list.
    """
    shipped = decode.PROFILE
    baseline = local_waveform.peaks_version(shipped)
    for field in fields(shipped):
        values = {f.name: getattr(shipped, f.name) for f in fields(shipped)}
        values[field.name] = values[field.name] + 1
        moved = decode.DecodeProfile(**values)
        assert local_waveform.peaks_version(moved) != baseline, (
            f"changing DecodeProfile.{field.name} left the version at {baseline!r}"
        )


def test_a_ragged_band_payload_is_refused_rather_than_guessed_at(source: Path) -> None:
    key = local_waveform._decode_key(source)
    ragged = np.arange(601, dtype=np.uint8)  # not a whole number of 3-band columns
    local_waveform._write_json(
        local_waveform._entry_path(SID),
        {
            "schema": rb_config.LOCAL_WAVEFORM_CACHE_SCHEMA,
            "peaks_version": local_waveform.peaks_version(),
            "peaks_b64": base64.b64encode(ragged.tobytes()).decode("ascii"),
            **key,
        },
    )
    assert local_waveform._cached_peaks(SID, key) is None


def test_the_stored_entry_records_the_version_it_was_written_under(source: Path) -> None:
    key = local_waveform._decode_key(source)
    local_waveform._store_peaks(SID, key, _tri_peaks())
    entry = json.loads(local_waveform._entry_path(SID).read_text(encoding="utf-8"))
    assert entry["peaks_version"] == local_waveform.peaks_version()
    assert entry["schema"] == rb_config.LOCAL_WAVEFORM_CACHE_SCHEMA
    assert str(decode.CROSSOVER_LOW_HZ) in entry["peaks_version"], (
        "the crossovers have to be readable off a cached entry, or a later "
        "round cannot say which graph produced the peaks it is scoring"
    )


def test_the_strip_is_the_360_byte_contract_and_is_never_padded() -> None:
    loud_then_quiet = np.concatenate(
        [
            np.full((600, decode.BAND_COUNT), 240, dtype=np.uint8),
            np.full((600, decode.BAND_COUNT), 30, dtype=np.uint8),
        ]
    )
    preview_b64, preview_max = local_waveform._strip_from_peaks(loud_then_quiet)
    assert preview_b64 is not None, "a full-length strip always encodes"
    raw = base64.b64decode(preview_b64)
    # On rekordbox PWV6's scale (NATIVE-22) the high band leads after pwv6_scale.
    assert len(raw) == 360 and preview_max == 255
    assert max(raw[:180]) == 255 and max(raw[180:]) == 41

    too_short = np.full(
        (local_waveform.STRIP_COLUMNS - 1, decode.BAND_COUNT), 240, dtype=np.uint8
    )
    assert local_waveform._strip_from_peaks(too_short) == (None, None), (
        "padding audio too short to fill the strip would be invented data"
    )


def test_the_payload_declares_tri_and_carries_three_distinct_bands() -> None:
    peaks = _tri_peaks(columns=1800)
    payload = build_waveform_payload(peaks, points=600)
    assert payload["kind"] == "tri"
    for section in ("preview", "detail"):
        bands = payload[section]
        assert bands["length"] > 0
        assert not (bands["low"] == bands["mid"] == bands["high"]), (
            f"{section} bands are identical, which is the mono claim, not tri"
        )
    assert payload["preview"]["length"] == min(600, decode.OVERVIEW_COLUMNS), (
        "the preview is capped at the rekordbox PWV6 width before `points` applies"
    )


@pytest.mark.parametrize("forced", ["engine", "ffmpeg", "rust"])
def test_a_forced_decoder_that_is_missing_never_serves_another_decoders_cache(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forced: str
) -> None:
    """Only ``auto`` falls back to the last real decode when nothing can decode.

    A forced decoder that is missing, or an invalid setting, must fail closed,
    not quietly serve columns another decoder wrote.
    """
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(tmp_path / "no-such-odj-audio"))
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    written = {**local_waveform._source_key(source), "decoder": "ffmpeg"}
    local_waveform._store_peaks(SID, written, _tri_peaks())

    # Control: under auto with no decoder at all, that same entry still stands.
    monkeypatch.delenv(decode.DECODER_ENV, raising=False)
    assert local_waveform._cached_peaks(SID, local_waveform._decode_key(source)) is not None

    monkeypatch.setenv(decode.DECODER_ENV, forced)
    assert local_waveform._cached_peaks(SID, local_waveform._decode_key(source)) is None


def test_ffmpeg_peaks_for_an_engine_refused_file_never_answer_a_forced_engine(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An auto decode the engine refused is stored as ffmpeg's. It stands in for
    the next auto engine request, but a forced engine must miss and try the
    engine itself."""
    engine_key = {**local_waveform._source_key(source), "decoder": "engine"}
    local_waveform._store_peaks(
        SID, {**engine_key, "decoder": "ffmpeg", "engine_refused": True}, _tri_peaks()
    )

    monkeypatch.delenv(decode.DECODER_ENV, raising=False)
    assert local_waveform._cached_peaks(SID, engine_key) is not None, "auto must reuse it"

    monkeypatch.setenv(decode.DECODER_ENV, "engine")
    assert local_waveform._cached_peaks(SID, engine_key) is None

    # Control: plain ffmpeg peaks (no engine refusal) never stand in for the engine.
    local_waveform._store_peaks(SID, {**engine_key, "decoder": "ffmpeg"}, _tri_peaks())
    monkeypatch.delenv(decode.DECODER_ENV)
    assert local_waveform._cached_peaks(SID, engine_key) is None


# ----- issue #13266: Windows-safe source keys (PLATFORM-13595) -----


@pytest.mark.requirement("PLATFORM-13595")
def test_source_key_includes_inode_when_stat_reports_nonzero(source: Path) -> None:
    """[if] POSIX stat has inode data [then] key includes inode, [else stop]."""
    real = source.stat()
    if real.st_ino == 0:
        pytest.skip("host reports inode 0; production omits inode the same way")
    key = local_waveform._source_key(source)
    assert key.get("inode") == real.st_ino


@pytest.mark.requirement("PLATFORM-13595")
def test_source_key_omits_inode_when_stat_lacks_inode_data(source: Path) -> None:
    """[if] stat lacks inode data [then] key omits inode, [else stop]."""
    real = source.stat()
    fake = SimpleNamespace(st_size=real.st_size, st_mtime_ns=real.st_mtime_ns)
    key_a = local_waveform._source_key(source, stat=fake)
    key_b = local_waveform._source_key(source, stat=fake)
    assert key_a == key_b
    assert "inode" not in key_a


@pytest.mark.requirement("PLATFORM-13595")
def test_source_key_changes_when_bytes_or_mtime_change(source: Path) -> None:
    """[if] bytes or mtime change [then] key changes, [else stop]."""
    before = local_waveform._source_key(source)
    with source.open("r+b") as handle:
        handle.seek(8)
        handle.write(b"\xff")
    after_bytes = local_waveform._source_key(source)
    assert after_bytes["content_fp"] != before["content_fp"]

    real = source.stat()
    new_mtime_ns = real.st_mtime_ns + 1_000_000_000
    os.utime(source, ns=(real.st_atime_ns, new_mtime_ns))
    after_mtime = local_waveform._source_key(source)
    assert after_mtime["mtime_ns"] == new_mtime_ns
    assert after_mtime != before
