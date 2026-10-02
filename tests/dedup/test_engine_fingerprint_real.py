"""Duplicate detection on the real engine: ``odj-audio fingerprint`` end to end.

[if] the engine fingerprints real audio [then] copies group and other music does not, [else stop].

Builds odj-audio from this checkout and pins it with ``ODJ_AUDIO_BIN`` (see
``tests/rust_build_env.py``; the contracts job runs this file with
``MDT_REQUIRE_AUDIO_ENGINE_BUILD=1`` so a missing toolchain fails there).
The audio is synthesized here, long enough (20 s) for a real fingerprint,
so every claim below is about chromaprint's actual output, not a fake.
"""
from __future__ import annotations

import math
import shutil
import sqlite3
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from apps.dedup import library_scan
from apps.engine_core.audio_engine import BIN_ENV
from apps.shared import fingerprints as fp_mod
from apps.shared import platform_paths
from apps.shared.fingerprints import compare, compute, match
from tests.rust_build_env import build_audio_engine

pytestmark = pytest.mark.requirement("META-09")


@pytest.fixture(scope="module")
def engine_bin() -> Path:
    return build_audio_engine(platform_paths.PROJECT_ROOT / "apps" / "audio-engine")


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, engine_bin: Path) -> Path:
    monkeypatch.setenv(BIN_ENV, str(engine_bin))
    # Never fall back to fpcalc in these tests: the engine must answer.
    monkeypatch.setattr(fp_mod, "_require_acoustid", _no_fpcalc)
    return engine_bin


def _no_fpcalc():
    raise AssertionError("the fpcalc fallback ran; the engine should have answered")


def _melody(seconds: float, rate: int, *, seed: int, gain: float = 0.6, lead_s: float = 0.0) -> list[float]:
    """A note every 0.25 s whose pitch follows ``seed``, so two seeds are
    different music and the chroma moves (silence fingerprints match
    anything)."""
    out = [0.0] * int(lead_s * rate)
    n = int(seconds * rate)
    for i in range(n):
        t = i / rate
        step = int(t * 4)
        semis = (step * 7 + seed * (step % 5) + seed) % 24
        f = 110.0 * 2 ** (semis / 12)
        out.append(gain * (0.6 * math.sin(2 * math.pi * f * t) + 0.3 * math.sin(2 * math.pi * 2 * f * t)))
    return out


def _wav(path: Path, samples: list[float], rate: int) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples))
    return path


@pytest.fixture(scope="module")
def tracks(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("fp-audio")
    return {
        "a": _wav(d / "a.wav", _melody(20, 44_100, seed=1), 44_100),
        # The same music: quieter and at another sample rate.
        "a_copy": _wav(d / "a-copy.wav", _melody(20, 48_000, seed=1, gain=0.3), 48_000),
        # The same music behind 1.5 s of silence.
        "a_late": _wav(d / "a-late.wav", _melody(20, 44_100, seed=1, lead_s=1.5), 44_100),
        # Different music.
        "b": _wav(d / "b.wav", _melody(20, 44_100, seed=4), 44_100),
    }


def test_same_audio_matches_and_different_audio_does_not(engine: Path, tracks: dict[str, Path]) -> None:
    """[if] the same music is re-encoded [then] it scores >= 0.92 and different music < 0.8, [else stop]."""
    a, a_copy, b = (compute(tracks[k]) for k in ("a", "a_copy", "b"))
    assert a.duration == pytest.approx(20.0, abs=0.01)
    assert a.fp_str.startswith("AQAA"), "algorithm 1 (fpcalc's default), the same format fpcalc prints"
    assert compare(a, a_copy) >= 0.92
    # The control that can say no: different music scores clearly below the
    # clustering threshold, through the same instrument.
    assert compare(a, b) < 0.8


def test_late_start_is_found_at_its_offset(engine: Path, tracks: dict[str, Path]) -> None:
    """[if] a copy starts 1.5 s later [then] match() finds it at a nonzero offset, [else stop]."""
    a, late = compute(tracks["a"]), compute(tracks["a_late"])
    sim, offset = match(a, late)
    assert sim >= 0.92
    assert offset != 0
    assert compare(a, late) < sim


def test_engine_and_fpcalc_fingerprints_are_interchangeable(engine: Path, tracks: dict[str, Path]) -> None:
    """[if] fpcalc fingerprinted the same file [then] it compares >= 0.98 against the engine's, [else stop].

    A cache filled by fpcalc on a dev machine compares against engine rows.
    """
    if shutil.which("fpcalc") is None:
        pytest.skip("UNAVAILABLE: fpcalc is not installed here, so cross-backend parity is unmeasured")
    ours = compute(tracks["a"])
    theirs = subprocess.run(
        ["fpcalc", "-plain", str(tracks["a"])], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert compare(ours, theirs) >= 0.98


def test_a_file_the_engine_cannot_read_fails_that_file_only(engine: Path, tmp_path: Path) -> None:
    """[if] a file is not audio [then] FingerprintFailed names it, not ChromaprintMissing, [else stop]."""
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio")
    with pytest.raises(fp_mod.FingerprintFailed):
        compute(junk)


def _state_db(path: Path, rows: list[tuple[str, Path | str, str | None]]) -> Path:
    from apps.shared.state.schema import apply_migrations

    conn = sqlite3.connect(path)
    apply_migrations(conn)
    for sid, file_path, deleted_at in rows:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
            "duration_ms, file_path, created_at, updated_at, deleted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, "inferred", sid, "[]", 20_000, str(file_path),
             "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00", deleted_at),
        )
    conn.commit()
    conn.close()
    return path


def test_library_scan_groups_the_library_duplicates(
    engine: Path, tracks: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the library holds three copies of one song [then] one group of exactly those, files untouched, [else stop]."""
    monkeypatch.setattr(platform_paths, "MUSIC_ROOTS", [tmp_path])
    state = _state_db(
        tmp_path / "state.db",
        [
            ("sid-a", tracks["a"], None),
            ("sid-a-copy", tracks["a_copy"], None),
            ("sid-a-late", tracks["a_late"], None),
            ("sid-b", tracks["b"], None),
            ("sid-gone", tmp_path / "missing.wav", None),
            # A deleted track is not part of the library any more.
            ("sid-deleted", tracks["a"].with_name("deleted.wav"), "2026-10-01T00:00:00+00:00"),
        ],
    )
    shutil.copy(tracks["a"], tracks["a"].with_name("deleted.wav"))
    before = {p: p.read_bytes() for p in tracks.values()}
    db = tmp_path / "dedup.sqlite"
    monkeypatch.setattr(library_scan.paths, "DEDUP_CLUSTERS_CSV", tmp_path / "clusters.csv")
    monkeypatch.setattr(library_scan.paths, "DEDUP_MANUAL_REVIEW_CSV", tmp_path / "manual.csv")

    prog = library_scan.run_library_scan(state_db=state, db_path=db, workers=2)

    assert (prog.total, prog.computed, prog.not_local, prog.errors) == (5, 4, 1, 0)
    assert prog.clusters == 1
    with sqlite3.connect(db) as conn:
        canonical = conn.execute("SELECT canonical_stable_id FROM duplicate_clusters").fetchall()
        aliases = {r[0] for r in conn.execute("SELECT alias_stable_id FROM track_aliases")}
    members = {canonical[0][0], *aliases}
    assert members == {"sid-a", "sid-a-copy", "sid-a-late"}
    # Reading only: every audio file is byte-for-byte what it was.
    assert all(p.read_bytes() == b for p, b in before.items())

    # A second scan reuses every fingerprint.
    again = library_scan.run_library_scan(state_db=state, db_path=db, workers=2)
    assert (again.computed, again.cache_hits, again.clusters) == (0, 4, 1)


def test_a_moved_track_is_confirmed_by_its_recorded_audio(
    engine: Path, tracks: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a scanned track's file moves [then] its same-named new home matches and an impostor is vetoed, [else stop]."""
    from apps.reconcile import locate
    from apps.reconcile.fingerprint_evidence import FingerprintEvidence
    from apps.shared import audio_files

    lib = tmp_path / "lib"
    lib.mkdir()
    original = lib / "song.wav"
    shutil.copy(tracks["a"], original)
    monkeypatch.setattr(platform_paths, "MUSIC_ROOTS", [tmp_path])
    monkeypatch.setattr(library_scan.paths, "DEDUP_CLUSTERS_CSV", tmp_path / "clusters.csv")
    monkeypatch.setattr(library_scan.paths, "DEDUP_MANUAL_REVIEW_CSV", tmp_path / "manual.csv")
    db = tmp_path / "dedup.sqlite"
    library_scan.run_library_scan(
        state_db=_state_db(tmp_path / "state.db", [("sid-song", original, None)]), db_path=db, workers=1
    )

    # The file moves; a different song with the same name sits elsewhere.
    moved = tmp_path / "moved" / "song.wav"
    moved.parent.mkdir()
    original.rename(moved)
    impostor = tmp_path / "other" / "song.wav"
    impostor.parent.mkdir()
    shutil.copy(tracks["b"], impostor)

    ev = FingerprintEvidence.open(db)
    assert ev is not None
    row = {"original_path": str(original), "basename": "song.wav", "stable_id": "sid-song",
           "title": "", "artist": "", "duration_s": "", "file_size": ""}
    idx = locate.FsIndex.build([audio_files.AudioFile(p, 1, 0.0, ".wav") for p in (moved, impostor)])
    cands = {c.path: c for c in locate.find_candidates(row, idx, {moved: None, impostor: None}, fingerprints=ev)}

    assert "fingerprint_match" in cands[moved].signals
    assert "fingerprint_mismatch" in cands[impostor].signals
    assert ev.unmeasured == 0
    assert next(iter(cands)) == moved, "the matching file ranks first"
