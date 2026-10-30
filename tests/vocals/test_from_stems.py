"""from-stems: derive vocal-cache from existing stem bundles (CPU, no demucs)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

try:
    import soundfile as sf
except ImportError:  # the requires_audio_stack marker below reports it UNAVAILABLE (Codex on #6821)
    sf = None

from apps.stems.artifacts import STEM_PARTS, load_stem_bundle
from apps.stems.bundle_lock import bundle_lock, bundle_lock_path, replace_bundle_dir
from apps.vocals import cache as vcache
from apps.vocals import from_stems as vfrom_stems
from tests.stems import fifo_part

pytestmark = [
    pytest.mark.requirement("CAT-05"),
    pytest.mark.requires_audio_stack,
]


def _write_v1_bundle(
    root: Path, stable_id: str, *, sr: int = 44100, seconds: int = 4, sung: tuple[int, int] = (1, 3)
) -> Path:
    """Aligned WAV bundle: vocals loud over ``sung`` seconds, accompaniment quiet."""
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    n = sr * seconds
    t = np.arange(n, dtype=np.float32) / sr
    vocals = np.zeros(n, dtype=np.float32)
    start, end = sung[0] * sr, sung[1] * sr
    vocals[start:end] = 0.4 * np.sin(2 * np.pi * 440 * t[start:end])
    quiet = 0.02 * np.sin(2 * np.pi * 110 * t)
    for part, mono in (
        ("vocals", vocals),
        ("drums", quiet),
        ("bass", quiet),
        ("other", quiet),
    ):
        sf.write(str(bundle / f"{part}.wav"), mono, sr, subtype="PCM_16")
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {
            "path": "/tmp/source.wav",
            "sha256": "a" * 64,
        },
        "files": {p: f"{p}.wav" for p in STEM_PARTS},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def test_derive_worker_result_finds_mid_track_vocals(tmp_path: Path) -> None:
    sid = "abc123fromstems000000000000000000000001"
    _write_v1_bundle(tmp_path, sid)
    bundle = load_stem_bundle(sid, stems_dir=tmp_path)
    result = vfrom_stems.derive_worker_result(bundle)
    assert result["source"] == vcache.VOCAL_CACHE_SOURCE
    assert result["schema"] == vcache.VOCAL_CACHE_SCHEMA
    assert result["params"]["derived_from_stems"] is True
    assert result["device"] == "from-stems"
    assert result["regions"], "expected at least one vocal region"
    # Mid-track sine should land near [1s, 3s] after hysteresis/merge.
    starts = [r["start_s"] for r in result["regions"]]
    ends = [r["end_s"] for r in result["regions"]]
    assert min(starts) <= 1.5
    assert max(ends) >= 2.5


def test_write_from_bundle_publishes_valid_cache(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    stems = data_dir / "state" / "stems"
    sid = "abc123fromstems000000000000000000000002"
    _write_v1_bundle(stems, sid)
    audio = tmp_path / "source.wav"
    # Source identity for cache invalidation; content need not match stems.
    sf.write(str(audio), np.zeros(44100, dtype=np.float32), 44100, subtype="PCM_16")
    entry = vfrom_stems.write_from_bundle(data_dir, sid, audio)
    loaded = vcache.load_valid_entry(vcache.cache_path(data_dir, sid), audio)
    assert loaded is not None
    assert loaded["coverage_pct"] == entry["coverage_pct"]
    assert loaded["regions"]


def _swap_writer(bundle: Path, fresh: Path) -> Callable[[], None]:
    """A real writer: swap ``fresh`` in under the bundle lock, as every publisher does."""

    def write() -> None:
        with bundle_lock(bundle):
            replace_bundle_dir(fresh, bundle)

    return write


@pytest.mark.skipif(not fifo_part.HAS_FIFO, reason=fifo_part.SKIP_REASON)
def test_write_from_bundle_decodes_outside_the_lock_and_discards_a_mid_decode_swap(
    tmp_path: Path,
) -> None:
    """[if] a writer swaps the bundle while its parts decode [then] that decode is discarded and the new generation is decoded, [else stop].

    The decode used to hold the bundle lock, so every hydrate or publish of
    an id on the same stripe waited it out (Codex on #6821); without the
    lock, a swap between two parts would mix generations under the old
    identity (Codex on #6820). A real writer takes the lock the moment the
    load lets go of it (tests/stems/fifo_part.py), so it lands inside a
    30 s bundle's real decode (about 70 ms here) unless the decode still
    holds the lock. Control: the published coverage is the NEW generation's.

    MUTATION TARGET: the pre-fix source, decode under ``held_for_read`` with
    no re-check (the writer lands after the decode and the swapped-out
    generation's coverage is published).
    """
    data_dir = tmp_path / "data"
    stems = data_dir / "state" / "stems"
    sid = "abc123fromstems000000000000000000000003"
    _write_v1_bundle(stems, sid, seconds=30, sung=(1, 2))
    old = vfrom_stems.derive_worker_result(load_stem_bundle(sid, stems_dir=stems))
    fresh = _write_v1_bundle(tmp_path / "fresh", sid, seconds=30, sung=(1, 20))
    expected = vfrom_stems.derive_worker_result(load_stem_bundle(sid, stems_dir=fresh.parent))
    assert expected["coverage_pct"] != old["coverage_pct"], "control: the generations differ"
    audio = tmp_path / "source.wav"
    sf.write(str(audio), np.zeros(44100, dtype=np.float32), 44100, subtype="PCM_16")

    with fifo_part.after_the_lock_is_released(
        bundle_lock_path(data_dir, sid), _swap_writer(stems / sid, fresh)
    ) as served:
        entry = vfrom_stems.write_from_bundle(data_dir, sid, audio)

    assert not served.errors, served.errors
    assert served.writers_run == 1
    assert entry["coverage_pct"] == expected["coverage_pct"], "the swapped-out generation was published"
    assert not list((data_dir / "state" / ".stem-publish").glob("waveform-pin-*")), "a decode left its pins behind"


def test_a_pinned_generation_decodes_its_own_parts_after_a_swap(tmp_path: Path) -> None:
    """[if] the bundle is swapped after its parts are pinned [then] the pinned bundle still decodes the old generation, [else stop].

    The generation marks cannot see a swap out and back on Windows, where
    a directory's ``st_ctime`` is its creation time (Codex on #6820), so the
    unlocked decode reads hard links taken under the lock, which keep
    naming the inodes the lock saw. Control: the canonical paths decode
    the new generation, and the pin directory goes once removed.

    MUTATION TARGET: return the canonical bundle from ``_pin_generation``
    even when its parts were pinned.
    """
    from apps.stems.stem_waveform import remove_waveform_pins

    data_dir = tmp_path / "data"
    stems = data_dir / "state" / "stems"
    sid = "abc123fromstems000000000000000000000004"
    _write_v1_bundle(stems, sid, seconds=8, sung=(1, 2))
    bundle = load_stem_bundle(sid, stems_dir=stems)
    old = vfrom_stems.derive_worker_result(bundle)
    identity = vfrom_stems.bundle_stem_identity(bundle)
    fresh = _write_v1_bundle(tmp_path / "fresh", sid, seconds=8, sung=(1, 7))

    scratch, pinned, _, _ = vfrom_stems._pin_generation(data_dir, sid, bundle, identity)
    assert scratch is not None, "the parts could not be linked on this volume"
    _swap_writer(stems / sid, fresh)()
    try:
        assert vfrom_stems.derive_worker_result(bundle)["coverage_pct"] != old["coverage_pct"], (
            "control: the canonical paths now hold the new generation"
        )
        assert vfrom_stems.derive_worker_result(pinned, identity=identity)["coverage_pct"] == old["coverage_pct"]
    finally:
        remove_waveform_pins(scratch)
    assert not scratch.exists()


@pytest.mark.skipif(not fifo_part.HAS_FIFO, reason=fifo_part.SKIP_REASON)
def test_parts_that_cannot_be_pinned_are_decoded_under_the_bundle_lock(tmp_path: Path) -> None:
    """[if] the parts cannot be hard-linked [then] every part is opened under the bundle lock, [else stop].

    Without links the decode reads the canonical paths, and on Windows a
    swap out and back can leave the generation marks unchanged, so an
    unlocked decode could mix two generations (Codex P1 on #6821). Here
    the pin directory cannot be made (a file holds its name), and a real
    writer is offered at each open of a FIFO part while the lock is free.
    Control: where the parts can be pinned, the same writer lands mid-decode
    and the result is discarded.

    MUTATION TARGET: decode the canonical paths without the lock when
    ``pin_files`` returns no pin directory.
    """
    for pinnable in (False, True):
        data_dir = tmp_path / f"data-{pinnable}"
        stems = data_dir / "state" / "stems"
        sid = "abc123fromstems000000000000000000000005"
        _write_v1_bundle(stems, sid, seconds=4, sung=(1, 2))
        bundle = load_stem_bundle(sid, stems_dir=stems)
        fresh = _write_v1_bundle(tmp_path / f"fresh-{pinnable}", sid, seconds=4, sung=(1, 3))
        if not pinnable:
            (data_dir / "state" / ".stem-publish").write_bytes(b"not a directory")
        part = stems / sid / "other.wav"
        held = fifo_part.make_fifo(part)
        identity = vfrom_stems.bundle_stem_identity(bundle)
        with fifo_part.serving(part, held, bundle_lock_path(data_dir, sid), [_swap_writer(stems / sid, fresh)]) as served:
            if pinnable:
                with pytest.raises(vfrom_stems.BundleChangedError):
                    vfrom_stems.derive_from_generation(data_dir, sid, bundle, identity)
            else:
                result = vfrom_stems.derive_from_generation(data_dir, sid, bundle, identity)
        assert not served.errors, served.errors
        if pinnable:
            assert served.writers_run == 1, "control: an unlocked open gets the writer"
        else:
            assert served.opens_under_lock >= 1 and served.writers_run == 0, served
            assert result["regions"]


def _lock_is_free(data_dir: Path, stable_id: str) -> bool:
    """True when another holder can take the bundle's read lock within a few seconds."""
    import threading

    from apps.stems.bundle_lock import held_for_read

    taken = threading.Event()

    def take() -> None:
        with held_for_read(data_dir, stable_id):
            taken.set()

    probe = threading.Thread(target=take, daemon=True)
    probe.start()
    free = taken.wait(timeout=5)
    probe.join(timeout=60)
    return free


def test_the_vocal_cache_entry_is_written_under_the_lock_that_proved_its_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] write_from_bundle publishes a vocal-cache entry [then] the bundle lock is held across the last check and the write, [else stop].

    A writer that swapped the bundle in after the last check and before an
    unlocked write would leave an entry naming the old generation for the new
    stems, and readers validate it by source audio alone (Codex P1 on #6820).
    MUTATION TARGET: publish after ``held_for_read`` is released.
    """
    data_dir = tmp_path / "data"
    stems = data_dir / "state" / "stems"
    sid = "abc123fromstems000000000000000000000006"
    _write_v1_bundle(stems, sid)
    audio = tmp_path / "source.wav"
    sf.write(str(audio), np.zeros(44100, dtype=np.float32), 44100, subtype="PCM_16")
    real = vcache.write_entry
    free_at_write: list[bool] = []

    def probe_then_write(*args: Any, **kwargs: Any) -> dict:
        free_at_write.append(_lock_is_free(data_dir, sid))
        return real(*args, **kwargs)

    monkeypatch.setattr(vcache, "write_entry", probe_then_write)

    entry = vfrom_stems.write_from_bundle(data_dir, sid, audio)

    assert free_at_write == [False], "the entry was written with the bundle lock free"
    written = vcache.load_valid_entry(vcache.cache_path(data_dir, sid), audio)
    assert written is not None, "no valid entry was written"
    assert written["coverage_pct"] == entry["coverage_pct"]


def test_control_the_read_lock_probe_sees_a_free_lock(tmp_path: Path) -> None:
    from apps.stems.bundle_lock import held_for_read

    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    assert _lock_is_free(data_dir, "sid")
    with held_for_read(data_dir, "sid"):
        assert not _lock_is_free(data_dir, "sid")


def test_a_swap_after_a_decode_held_under_the_lock_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a writer swaps the bundle after a lock-held decode (no hard links) [then] that result is not published, [else stop].

    On a volume without hard links the decode runs under the lock, but its
    result used to be returned with no check after the lock was released, so
    a swap landing before the publish went unseen (Codex P1 on #6820).
    MUTATION TARGET: return the unpinned result without the final check.
    """
    data_dir = tmp_path / "data"
    stems = data_dir / "state" / "stems"
    sid = "abc123fromstems000000000000000000000007"
    _write_v1_bundle(stems, sid, sung=(1, 2))
    bundle = load_stem_bundle(sid, stems_dir=stems)
    identity = vfrom_stems.bundle_stem_identity(bundle)
    fresh = _write_v1_bundle(tmp_path / "fresh", sid, sung=(1, 3))
    (data_dir / "state" / ".stem-publish").write_bytes(b"not a directory")  # no pin directory: decode under the lock
    pin = vfrom_stems._pin_generation

    def decode_then_a_writer_swaps(*args: Any) -> Any:
        pinned = pin(*args)
        assert pinned[0] is None, "the parts were pinned: this is not the lock-held decode"
        _swap_writer(stems / sid, fresh)()  # lands once the decode lets go of the lock
        return pinned

    monkeypatch.setattr(vfrom_stems, "_pin_generation", decode_then_a_writer_swaps)
    published: list[dict] = []

    with pytest.raises(vfrom_stems.BundleChangedError):
        vfrom_stems.derive_from_generation(data_dir, sid, bundle, identity, publish=published.append)

    assert published == [], "the swapped-out generation was published"
    # Control: with no writer, the same lock-held decode publishes.
    monkeypatch.setattr(vfrom_stems, "_pin_generation", pin)
    now = load_stem_bundle(sid, stems_dir=stems)
    vfrom_stems.derive_from_generation(data_dir, sid, now, vfrom_stems.bundle_stem_identity(now), publish=published.append)
    assert len(published) == 1
