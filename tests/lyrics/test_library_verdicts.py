"""Library-scale stem-coverage lyric_verdict backfill (LYR-06).

- if the reserved-100 guard is dropped then a QA-held-back track gets a
  computed verdict instead of being refused -- broken
- if the has-words guard is dropped then a real ASR/aligner row's
  words_content_hash / n_words get NULLed by a coverage-only recompute --
  broken
- if a corrupt bundle is silently skipped instead of reported failed then an
  operator cannot tell "nothing to do" from "something is broken" -- broken
- if the resumability check stops comparing the bundle's own source hash
  then a re-run either loops forever recomputing unchanged bundles, or
  (worse) never notices a replaced bundle -- broken
- if a human override stops surviving this backfill then the human is not
  being obeyed -- broken

Fixtures are REAL audio: ffmpeg renders a 440Hz sine ("vocal" signal) and
true digital silence ("instrumental" signal) to MP3, written as demucs4
schema-v1 bundles -- the actual on-disk shape for 915 of the maintainer's 921
present-with-stems tracks (Mon 14 Sep 2026 measurement). This depends on PR
#2593 (v1 bundles load MP3/FLAC parts by their own container): every test
here is skipped, not failed, when that fix is not present in
apps.webui.server.stem_artifacts, so this suite is honest about its own
dependency rather than red for an unrelated reason. See
apps/lyrics/library_verdicts.py's module docstring for the merge-order note.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from apps.lyrics import library_verdicts, store
from apps.shared.state import db as state_db
from apps.vocals import cache as vcache
from apps.vocals import from_stems as vfrom_stems
from apps.webui.server.stem_artifacts import (
    STEM_PARTS,
    StemArtifactError,
    StemBundle,
    load_stem_bundle,
)

from .conftest import seed_track

SR = 44100
#: The loud-vocal fixture is split into a true-silent lead-in followed by a
#: tone, rather than one constant tone for the whole clip -- see
#: ``_render_segmented_mp3`` for why a constant ratio breaks the coverage
#: worker's adaptive threshold. The quiet-vocal fixture stays one constant
#: silent/loud clip each, so total duration must match either shape.
_LEAD_SILENCE_S = 1.0
_TONE_S = 2.0
DURATION_S = _LEAD_SILENCE_S + _TONE_S
_MP3_SUPPORTS_V1 = True
try:
    from apps.webui.server.stem_artifacts import read_stem_container_metadata  # noqa: F401
except ImportError:
    _MP3_SUPPORTS_V1 = False

requires_mp3_loader = pytest.mark.skipif(
    not _MP3_SUPPORTS_V1,
    reason=(
        "apps.webui.server.stem_artifacts does not yet load v1 MP3 parts; "
        "depends on PR #2593 (fix(stems): v1 bundles load MP3 and FLAC parts "
        "by their own container), which must merge before this suite is green"
    ),
)
requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH"
)
#: mode-000 only blocks reads for a NON-root process -- root bypasses
#: filesystem permission checks entirely (the kernel, not this project's
#: code, decides that), so any fixture built on ``os.chmod(0o000)`` cannot
#: be produced for real under root. Rebutted rather than mocked: skipped in
#: that one environment, stating why, while sibling real fixtures (a
#: removed file, a directory where a file is expected) still exercise the
#: same code path. Defined once here (not per-use-site) so every skipif
#: below shares the SAME probe.
_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
#: Tests that render real MP3 fixtures AND run them through the actual
#: coverage decode (``derive_worker_result``) need all three: ffmpeg to
#: render the fixture, PR #2593 to load it, and the ``analysis`` extra
#: (soundfile/numpy) to decode it. Tests that only exercise a guard which
#: short-circuits BEFORE loading (reserved, has-words) or before decoding
#: (dry-run, corrupt bundle) need a strict subset -- applied individually per
#: test below so a missing ``analysis`` extra never hides an otherwise-
#: runnable guard test behind an unrelated skip.


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Shadows ``tests.lyrics.conftest.data_dir``: a data dir whose
    ``state/`` exists but holds NO reserved-100 file, which is what a fresh
    install looks like, so every backfill in this suite runs the default
    ``include_reserved=False`` path over an absent reservation. Tests about a
    genuinely reserved track write the file themselves."""
    path = tmp_path / "data"
    (path / "state").mkdir(parents=True, exist_ok=True)
    return path


#-----------------------------------------------------------------------------
# fixtures: real ffmpeg-rendered MP3 stem bundles
#-----------------------------------------------------------------------------
def _render_mp3(path: Path, *, silent: bool) -> None:
    source = (
        f"anullsrc=r={SR}:cl=stereo:d={DURATION_S}"
        if silent
        else f"sine=frequency=440:duration={DURATION_S}:sample_rate={SR}"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", source,
            "-ac", "2", "-codec:a", "libmp3lame", "-b:a", "128k",
            str(path),
        ],
        check=True,
    )


def _render_segmented_mp3(path: Path) -> None:
    """True silence for ``_LEAD_SILENCE_S``, then a loud tone for ``_TONE_S``.

    ``scripts/vocal_region_worker.py::thresholds_for_ratio`` only trusts the
    calibrated 0.10/0.05 hysteresis pair when the vocals/mix ratio envelope
    dips below ``OFF_RATIO`` at least once. A clip that is a tone for its
    entire duration never dips -- the mix IS the vocals stem when the other
    three parts are true silence, so the ratio is a constant ~1.0 -- and
    ``thresholds_for_ratio`` reads that as hysteresis stuck on and adapts a
    threshold ABOVE the envelope's own ceiling (base + ADAPT_MARGIN, where
    base is that same constant), so no region ever opens and coverage comes
    back 0.0 regardless of how loud the vocal actually is. A real demucs
    bundle almost never has this shape (leakage gives the ratio genuine
    variance); this fixture reproduces that variance directly so the loud
    tone is actually detected as vocal presence rather than accidentally
    exercising the adapted-threshold escape hatch.
    """
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=stereo:d={_LEAD_SILENCE_S}",
            "-f", "lavfi", "-i",
            f"sine=frequency=440:duration={_TONE_S}:sample_rate={SR}",
            "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]",
            "-map", "[out]",
            "-ac", "2", "-codec:a", "libmp3lame", "-b:a", "128k",
            str(path),
        ],
        check=True,
    )


def _write_bundle(
    root: Path, stable_id: str, *, vocal_loud: bool, source_sha256: str = "a" * 64
) -> Path:
    """A demucs4 v1 bundle: vocals loud/silent, the other three parts the
    opposite, so coverage lands unambiguously on one side of both bands
    (NO_LYRICS_MAX_COVERAGE 12.5 / SPARSE_MAX_COVERAGE 25.0). The loud-vocal
    case uses :func:`_render_segmented_mp3` (see its docstring): a constant
    tone for the whole clip pins the vocals/mix ratio at a constant value,
    which the coverage worker misreads as leakage-stuck-on and silently
    zeroes out."""
    bundle_dir = root / stable_id
    bundle_dir.mkdir(parents=True)
    for part in STEM_PARTS:
        loud = vocal_loud if part == "vocals" else not vocal_loud
        if part == "vocals" and loud:
            _render_segmented_mp3(bundle_dir / f"{part}.mp3")
        else:
            _render_mp3(bundle_dir / f"{part}.mp3", silent=not loud)
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": f"/music/{stable_id}.flac", "sha256": source_sha256},
        "files": {part: f"{part}.mp3" for part in STEM_PARTS},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle_dir


def _write_corrupt_bundle(root: Path, stable_id: str) -> Path:
    """A manifest whose vocals.mp3 has no MPEG sync word at all."""
    bundle_dir = root / stable_id
    bundle_dir.mkdir(parents=True)
    for part in STEM_PARTS:
        (bundle_dir / f"{part}.mp3").write_bytes(b"not an mp3 frame at all")
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": f"/music/{stable_id}.flac", "sha256": "b" * 64},
        "files": {part: f"{part}.mp3" for part in STEM_PARTS},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle_dir


def _write_wav_bundle(
    root: Path, stable_id: str, *, duration_s: float = 1.0, sr: int = 8000,
    source_sha256: str = "a" * 64,
) -> Path:
    """A stdlib-only (``wave`` module) v1 bundle: silence in every part.

    Unlike :func:`_write_bundle`, this never depends on ffmpeg or on PR
    #2593's MP3/FLAC loader -- WAV parts already load on main -- so tests
    using it run in every environment, including this repo's ``dev``-extra
    venv, which has neither ffmpeg nor the ``analysis`` extra installed.
    Content is irrelevant: these bundles only feed the vocal-cache IDENTITY
    guard (:func:`apps.lyrics.library_verdicts._cached_coverage_pct`), never
    a real coverage decode (which needs ``soundfile``, unavailable here).
    """
    bundle_dir = root / stable_id
    bundle_dir.mkdir(parents=True)
    n_frames = int(duration_s * sr)
    for part in STEM_PARTS:
        with wave.open(str(bundle_dir / f"{part}.wav"), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sr)
            wav_file.writeframes(b"\x00\x00" * n_frames)
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": f"/music/{stable_id}.flac", "sha256": source_sha256},
        "files": {part: f"{part}.wav" for part in STEM_PARTS},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle_dir


def _stems_root(data_dir: Path) -> Path:
    return data_dir / "state" / "stems"


def _write_bundle_dir_stub(root: Path, stable_id: str) -> Path:
    """An empty bundle DIRECTORY, no manifest, no audio.

    ``candidate_stable_ids`` only lists directory names -- :func:`load_stem_bundle`
    is what actually validates a bundle, and the reserved / has-words guards
    both short-circuit BEFORE this module ever calls it. A test that only
    exercises one of those two guards gets a real green result today, with no
    dependency on ffmpeg or PR #2593's MP3 loader, by using this instead of
    :func:`_write_bundle`.
    """
    bundle_dir = root / stable_id
    bundle_dir.mkdir(parents=True)
    return bundle_dir


#-----------------------------------------------------------------------------


@requires_ffmpeg
@requires_mp3_loader
@pytest.mark.requires_audio_stack
def test_backfill_writes_vocal_verdict_for_loud_vocal_bundle(conn, data_dir) -> None:
    seed_track(conn, "sid-loud-vocal-001")
    _write_bundle(_stems_root(data_dir), "sid-loud-vocal-001", vocal_loud=True)

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.failed == {}
    assert report.processed == ("sid-loud-vocal-001",)
    verdict = store.get_verdict(conn, "sid-loud-vocal-001")
    assert verdict is not None
    assert verdict.verdict == "vocal"
    assert verdict.coverage_pct is not None and verdict.coverage_pct > 25.0
    assert verdict.pipeline_version == library_verdicts.STEM_COVERAGE_PIPELINE_VERSION
    assert verdict.n_words is None and verdict.words_content_hash is None


@requires_ffmpeg
@requires_mp3_loader
@pytest.mark.requires_audio_stack
def test_backfill_writes_no_lyrics_verdict_for_silent_vocal_bundle(conn, data_dir) -> None:
    seed_track(conn, "sid-silent-vocal-001")
    _write_bundle(_stems_root(data_dir), "sid-silent-vocal-001", vocal_loud=False)

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.failed == {}
    verdict = store.get_verdict(conn, "sid-silent-vocal-001")
    assert verdict is not None
    assert verdict.verdict == "no-lyrics"
    assert verdict.coverage_pct is not None and verdict.coverage_pct <= 12.5


@requires_ffmpeg
@requires_mp3_loader
@pytest.mark.requires_audio_stack
def test_backfill_skips_a_human_override_and_reports_it(conn, data_dir) -> None:
    """P1 BLOCKING fix (Sol review round 4, PR #2611, library_verdicts.py:308):
    LYR-06's own acceptance criterion groups a human ``override`` with
    ``words_content_hash`` -- "the backfill never overwrites it, only
    fills/refreshes coverage-only rows". An overridden row is not a
    coverage-only row, so it must be excluded from a recompute ENTIRELY, not
    merely have ``override``/``override_note`` themselves survive underneath
    an otherwise-refreshed computed verdict."""
    seed_track(conn, "sid-override-001")
    _write_bundle(_stems_root(data_dir), "sid-override-001", vocal_loud=False)
    library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )
    store.set_override(
        conn, stable_id="sid-override-001", override="vocal", note="I heard singing"
    )
    original = store.get_verdict(conn, "sid-override-001")
    assert original is not None

    # A changed bundle would otherwise force a genuine recompute (new source
    # hash) -- the scenario that would clobber the computed fields under an
    # override if the guarantee ever broke.
    shutil.rmtree(_stems_root(data_dir) / "sid-override-001")
    _write_bundle(
        _stems_root(data_dir), "sid-override-001", vocal_loud=False,
        source_sha256="c" * 64,
    )
    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ()
    assert report.skipped.get(library_verdicts.SKIP_HAS_OVERRIDE) == ("sid-override-001",)
    verdict = store.get_verdict(conn, "sid-override-001")
    assert verdict is not None
    assert verdict.verdict == original.verdict, "the computed value is frozen, not refreshed"
    assert verdict.coverage_pct == original.coverage_pct
    assert verdict.source == original.source
    assert verdict.pipeline_version == original.pipeline_version
    assert verdict.computed_at == original.computed_at
    assert verdict.override == "vocal"
    assert verdict.override_note == "I heard singing"
    assert verdict.effective == "vocal", "the override still wins"


def test_backfill_skips_a_reserved_track_and_reports_it(conn, data_dir) -> None:
    """No ffmpeg / MP3-loader dependency: the reserved guard fires before the
    bundle is ever loaded."""
    seed_track(conn, "sid-reserved-001")
    _write_bundle_dir_stub(_stems_root(data_dir), "sid-reserved-001")
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / library_verdicts.RESERVED_FILENAME).write_text(
        json.dumps({"tracks": [{"stable_id": "sid-reserved-001"}]}), encoding="utf-8"
    )

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ()
    assert report.skipped.get(library_verdicts.SKIP_RESERVED) == ("sid-reserved-001",)
    assert store.get_verdict(conn, "sid-reserved-001") is None


@requires_ffmpeg
@requires_mp3_loader
@pytest.mark.requires_audio_stack
def test_include_reserved_flag_processes_a_reserved_track(conn, data_dir) -> None:
    seed_track(conn, "sid-reserved-002")
    _write_bundle(_stems_root(data_dir), "sid-reserved-002", vocal_loud=True)
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / library_verdicts.RESERVED_FILENAME).write_text(
        json.dumps({"tracks": [{"stable_id": "sid-reserved-002"}]}), encoding="utf-8"
    )

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=True
    )

    assert report.processed == ("sid-reserved-002",)
    assert store.get_verdict(conn, "sid-reserved-002") is not None


def test_backfill_reports_a_corrupt_bundle_as_a_failure(conn, data_dir) -> None:
    """No ffmpeg / MP3-loader dependency: the bundle is garbage on purpose,
    so this test is meaningful even before PR #2593 merges."""
    seed_track(conn, "sid-corrupt-001")
    _write_corrupt_bundle(_stems_root(data_dir), "sid-corrupt-001")

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ()
    assert "sid-corrupt-001" in report.failed
    assert store.get_verdict(conn, "sid-corrupt-001") is None
    with pytest.raises(StemArtifactError):
        load_stem_bundle("sid-corrupt-001", stems_dir=_stems_root(data_dir))


def test_backfill_never_clobbers_word_level_data(conn, data_dir) -> None:
    """No ffmpeg / MP3-loader dependency: the has-words guard fires before
    the bundle is ever loaded."""
    seed_track(conn, "sid-has-words-001")
    _write_bundle_dir_stub(_stems_root(data_dir), "sid-has-words-001")
    digest = "d" * 64
    store.upsert_verdict(
        conn,
        stable_id="sid-has-words-001",
        verdict="vocal",
        coverage_pct=90.0,
        source="lrclib get",
        language_iso3="eng",
        n_words=42,
        n_lines=6,
        pct_witness_red=0.1,
        pipeline_version="2026.09.09-round3a",
        words_content_hash=digest,
        computed_at="2026-09-01T00:00:00.000000+00:00",
        resurrect=False,
    )

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ()
    assert report.skipped.get(library_verdicts.SKIP_HAS_WORDS) == ("sid-has-words-001",)
    verdict = store.get_verdict(conn, "sid-has-words-001")
    assert verdict is not None
    assert verdict.n_words == 42
    assert verdict.words_content_hash == digest


def test_backfill_never_clobbers_an_overridden_row(conn, data_dir) -> None:
    """No ffmpeg / MP3-loader dependency: the has-override guard fires
    before the bundle is ever loaded, symmetric with
    ``test_backfill_never_clobbers_word_level_data`` above. P1 BLOCKING fix
    (Sol review round 4, PR #2611, library_verdicts.py:308)."""
    seed_track(conn, "sid-has-override-001")
    _write_bundle_dir_stub(_stems_root(data_dir), "sid-has-override-001")
    store.upsert_verdict(
        conn,
        stable_id="sid-has-override-001",
        verdict="no-lyrics",
        coverage_pct=3.0,
        source="stem-coverage:v1:" + "e" * 64,
        language_iso3=None,
        n_words=None,
        n_lines=None,
        pct_witness_red=None,
        pipeline_version=library_verdicts.STEM_COVERAGE_PIPELINE_VERSION,
        words_content_hash=None,
        computed_at="2026-09-01T00:00:00.000000+00:00",
        resurrect=False,
    )
    store.set_override(
        conn, stable_id="sid-has-override-001", override="vocal", note="I heard singing"
    )
    before = store.get_verdict(conn, "sid-has-override-001")
    assert before is not None

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ()
    assert report.skipped.get(library_verdicts.SKIP_HAS_OVERRIDE) == (
        "sid-has-override-001",
    )
    verdict = store.get_verdict(conn, "sid-has-override-001")
    assert verdict is not None
    assert verdict.verdict == before.verdict
    assert verdict.coverage_pct == before.coverage_pct
    assert verdict.computed_at == before.computed_at
    assert verdict.override == "vocal"
    assert verdict.effective == "vocal"


def test_backfill_never_clobbers_a_row_from_another_producer(conn, data_dir) -> None:
    """LYR-06 P1 BLOCKING fix (CLAUDE-review round 7, PR #2611,
    library_verdicts.py:216... store.py:336): a row with NULL
    ``words_content_hash`` and NULL ``override`` is NOT proof it is
    coverage-only. An ASR/karaoke "no-lyrics" verdict with no words fits
    that same test -- no words to hash, never overridden -- but it is a
    REAL judgement from a different producer. No ffmpeg / MP3-loader
    dependency: the source guard fires before the bundle is ever loaded,
    symmetric with ``test_backfill_never_clobbers_word_level_data`` and
    ``test_backfill_never_clobbers_an_overridden_row`` above."""
    seed_track(conn, "sid-other-source-001")
    _write_bundle_dir_stub(_stems_root(data_dir), "sid-other-source-001")
    store.upsert_verdict(
        conn,
        stable_id="sid-other-source-001",
        verdict="no-lyrics",
        coverage_pct=2.0,
        source="asr:no-lyrics-v2",
        language_iso3=None,
        n_words=None,
        n_lines=None,
        pct_witness_red=None,
        pipeline_version="asr-v2",
        words_content_hash=None,
        computed_at="2026-09-01T00:00:00.000000+00:00",
        resurrect=False,
    )
    before = store.get_verdict(conn, "sid-other-source-001")
    assert before is not None

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ()
    assert report.skipped.get(library_verdicts.SKIP_HAS_OTHER_SOURCE) == (
        "sid-other-source-001",
    )
    verdict = store.get_verdict(conn, "sid-other-source-001")
    assert verdict is not None
    assert verdict.verdict == before.verdict
    assert verdict.coverage_pct == before.coverage_pct
    assert verdict.source == before.source
    assert verdict.computed_at == before.computed_at


@requires_ffmpeg
@requires_mp3_loader
@pytest.mark.requires_audio_stack
def test_backfill_rerun_is_a_no_op(conn, data_dir) -> None:
    seed_track(conn, "sid-rerun-001")
    _write_bundle(_stems_root(data_dir), "sid-rerun-001", vocal_loud=True)
    first = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )
    assert first.processed == ("sid-rerun-001",)
    before = store.get_verdict(conn, "sid-rerun-001")
    assert before is not None

    second = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert second.processed == ()
    assert second.skipped.get(library_verdicts.SKIP_UP_TO_DATE) == ("sid-rerun-001",)
    after = store.get_verdict(conn, "sid-rerun-001")
    assert after is not None
    assert after.computed_at == before.computed_at, "nothing was recomputed"


@requires_ffmpeg
@requires_mp3_loader
@pytest.mark.requires_audio_stack
def test_a_replaced_bundle_forces_recompute(conn, data_dir) -> None:
    """The resumability key is the BUNDLE's source hash, not just presence."""
    seed_track(conn, "sid-replaced-001")
    _write_bundle(
        _stems_root(data_dir), "sid-replaced-001", vocal_loud=True,
        source_sha256="1" * 64,
    )
    library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )
    first_verdict = store.get_verdict(conn, "sid-replaced-001")
    assert first_verdict is not None and first_verdict.verdict == "vocal"

    shutil.rmtree(_stems_root(data_dir) / "sid-replaced-001")
    _write_bundle(
        _stems_root(data_dir), "sid-replaced-001", vocal_loud=False,
        source_sha256="2" * 64,
    )
    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ("sid-replaced-001",)
    second_verdict = store.get_verdict(conn, "sid-replaced-001")
    assert second_verdict is not None
    assert second_verdict.verdict == "no-lyrics"


#-----------------------------------------------------------------------------
# freshness key: the STEMS' own identity, not merely the source track's.
# Fast, stdlib-only (a WAV bundle + a matching vocal-cache entry per
# generation avoids both ffmpeg and a real soundfile decode).
#-----------------------------------------------------------------------------
def _mutate_manifest_model_version(bundle_dir: Path, new_version: str) -> None:
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model"]["version"] = new_version
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _rewrite_part_with_new_duration(
    bundle_dir: Path, part: str, *, duration_s: float, sr: int
) -> None:
    n_frames = int(duration_s * sr)
    path = bundle_dir / f"{part}.wav"
    mtime_before_ns = path.stat().st_mtime_ns
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sr)
        wav_file.writeframes(b"\x00\x00" * n_frames)
    # A real re-render lands later than the file it replaces. A rewrite inside the
    # same filesystem timestamp tick keeps the old mtime_ns, and at the same size
    # the part is byte-for-byte indistinguishable to `bundle_stem_identity` (main
    # push CI run 37119163338). Stamp the rewrite strictly later, as a repair is.
    later_ns = max(path.stat().st_mtime_ns, mtime_before_ns + 1_000_000_000)
    os.utime(path, ns=(later_ns, later_ns))


def _seed_matching_cache(
    data_dir: Path, stable_id: str, bundle: StemBundle, *, coverage_pct: float
) -> None:
    cache_path = vcache.cache_path(data_dir, stable_id)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema": vcache.VOCAL_CACHE_SCHEMA,
                "coverage_pct": coverage_pct,
                "duration_s": library_verdicts._bundle_duration_s(bundle),
                "params": {
                    "derived_from_stems": True,
                    "bundle_layout": bundle.layout,
                    "bundle_stem_sha256": vfrom_stems.bundle_stem_identity(bundle),
                },
            }
        ),
        encoding="utf-8",
    )


def test_freshness_recomputes_after_a_model_version_change(conn, data_dir) -> None:
    """P1 BLOCKING fix (Sol follow-up review, PR #2611, library_verdicts.py:171):
    the freshness key used to be ``bundle.manifest.source.sha256`` alone,
    which identifies only the ORIGINAL track -- a re-separation with a
    different model/version for the SAME source left it unchanged, so the
    old verdict was silently skipped forever. ``_bundle_source_label`` is now
    keyed on :func:`apps.vocals.from_stems.bundle_stem_identity`, which
    hashes ``manifest.json``'s own bytes (including ``model.version``) plus
    each part's stat -- so a manifest-only change, SAME source sha, forces a
    recompute."""
    seed_track(conn, "sid-fresh-model-001")
    stems_root = _stems_root(data_dir)
    bundle_dir = _write_wav_bundle(stems_root, "sid-fresh-model-001", source_sha256="3" * 64)
    bundle_v1 = load_stem_bundle("sid-fresh-model-001", stems_dir=stems_root)
    # Captured BEFORE the mutation below: bundle_v1.files are just Paths, so
    # calling bundle_stem_identity(bundle_v1) AFTER mutating would stat the
    # very same, by-then-mutated files on disk and defeat this comparison.
    identity_v1 = vfrom_stems.bundle_stem_identity(bundle_v1)
    _seed_matching_cache(data_dir, "sid-fresh-model-001", bundle_v1, coverage_pct=10.0)

    first = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )
    assert first.processed == ("sid-fresh-model-001",)
    assert store.get_verdict(conn, "sid-fresh-model-001").coverage_pct == 10.0

    # Re-separate with a DIFFERENT model version, SAME source track.
    _mutate_manifest_model_version(bundle_dir, "4.0.2")
    bundle_v2 = load_stem_bundle("sid-fresh-model-001", stems_dir=stems_root)
    assert bundle_v2.manifest.source.sha256 == bundle_v1.manifest.source.sha256
    assert vfrom_stems.bundle_stem_identity(bundle_v2) != identity_v1
    _seed_matching_cache(data_dir, "sid-fresh-model-001", bundle_v2, coverage_pct=77.0)

    second = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert second.processed == ("sid-fresh-model-001",), (
        "a model-version re-separation must recompute, not skip as up-to-date"
    )
    assert store.get_verdict(conn, "sid-fresh-model-001").coverage_pct == 77.0


def test_freshness_recomputes_after_a_part_file_change(conn, data_dir) -> None:
    """Same fix, the OTHER named trigger: a repaired/re-rendered part with
    ``manifest.json`` bytes UNCHANGED must also force a recompute --
    ``bundle_stem_identity`` hashes each part's own ``(filename, size,
    mtime_ns)`` too, not only ``manifest.json``. The repair keeps the SAME
    duration (a plausible "reprocessed, not re-timed" repair) and every OTHER
    part untouched, so this isolates the single-part-mtime signal on its
    own -- a same-duration rewrite still changes ``mtime_ns``, and v1's exact
    per-part frame-count-alignment rule (``stem_artifacts.
    _validate_v1_part_metadata``) means a differently-timed WAV part would
    fail to load at all, which is not what this test is exercising."""
    seed_track(conn, "sid-fresh-part-001")
    stems_root = _stems_root(data_dir)
    bundle_dir = _write_wav_bundle(stems_root, "sid-fresh-part-001", source_sha256="4" * 64)
    manifest_before = (bundle_dir / "manifest.json").read_bytes()
    bundle_v1 = load_stem_bundle("sid-fresh-part-001", stems_dir=stems_root)
    # Captured BEFORE the mutation below -- see the sibling model-version
    # test for why a post-mutation read of bundle_v1 would defeat this.
    identity_v1 = vfrom_stems.bundle_stem_identity(bundle_v1)
    _seed_matching_cache(data_dir, "sid-fresh-part-001", bundle_v1, coverage_pct=11.0)

    first = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )
    assert first.processed == ("sid-fresh-part-001",)

    # Repair ONE part (re-rendered, SAME duration so v1's exact per-part
    # frame-count alignment still holds); manifest.json bytes untouched.
    _rewrite_part_with_new_duration(bundle_dir, "vocals", duration_s=1.0, sr=8000)
    assert (bundle_dir / "manifest.json").read_bytes() == manifest_before
    bundle_v2 = load_stem_bundle("sid-fresh-part-001", stems_dir=stems_root)
    assert vfrom_stems.bundle_stem_identity(bundle_v2) != identity_v1
    _seed_matching_cache(data_dir, "sid-fresh-part-001", bundle_v2, coverage_pct=88.0)

    second = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert second.processed == ("sid-fresh-part-001",), (
        "a repaired part must force a recompute, not skip as up-to-date"
    )
    assert store.get_verdict(conn, "sid-fresh-part-001").coverage_pct == 88.0


def test_freshness_skips_an_unchanged_bundle(conn, data_dir) -> None:
    """Opposite-direction control: nothing about the bundle changed between
    two runs, so the second run must skip it as up-to-date, not recompute --
    stdlib-only sibling of ``test_backfill_rerun_is_a_no_op`` (which needs
    the full audio stack), so this direction is covered without it too."""
    seed_track(conn, "sid-fresh-unchanged-001")
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-fresh-unchanged-001", source_sha256="5" * 64)
    bundle = load_stem_bundle("sid-fresh-unchanged-001", stems_dir=stems_root)
    _seed_matching_cache(data_dir, "sid-fresh-unchanged-001", bundle, coverage_pct=9.0)

    first = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )
    assert first.processed == ("sid-fresh-unchanged-001",)

    second = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert second.processed == ()
    assert second.skipped.get(library_verdicts.SKIP_UP_TO_DATE) == ("sid-fresh-unchanged-001",)


#-----------------------------------------------------------------------------
# post-load filesystem faults (Sol review round 6, PR #2611): after
# load_stem_bundle succeeds, _bundle_source_label / _cached_coverage_pct
# re-stat the manifest and every part file a SECOND time (see
# bundle_stem_identity's docstring). A part removed or made unreadable in
# that interval must be reported as a per-track FAILURE, never abort the
# whole backfill and never be silently skipped. Every fault below is REAL:
# a genuine successful load followed by a real os.remove()/chmod(0o000) on
# the same bundle -- no monkeypatching load_stem_bundle, _bundle_source_
# label, or bundle_stem_identity anywhere in this section. Single-threaded
# Python has no way to pause a synchronous _scan_candidate/backfill_verdicts
# call BETWEEN its internal load and its internal re-stat to inject the
# mutation mid-flight without a timing race (rejected: flaky, and this
# project's no-mocks rule bars faking the scheduling point instead) -- so
# these tests call the exact post-load seams _scan_candidate/backfill_
# verdicts are built from (_classify_loaded_bundle, _write_candidate)
# directly, with the real mutation performed in between, proving the SAME
# production code the orchestration calls internally.
#-----------------------------------------------------------------------------
def test_classify_loaded_bundle_reports_a_vanished_part_as_a_failure(data_dir) -> None:
    """P1 BLOCKING fix (Sol review round 6, PR #2611, library_verdicts.py
    _scan_candidate / _bundle_source_label): a part removed after a real,
    successful load must come back as _ScanOutcome.fail_reason, never raise
    and propagate out of the scan phase (which would abort the whole run for
    every OTHER candidate too)."""
    stems_root = _stems_root(data_dir)
    bundle_dir = _write_wav_bundle(stems_root, "sid-vanish-scan-001", source_sha256="1" * 64)
    # a REAL, successful load
    bundle = load_stem_bundle("sid-vanish-scan-001", stems_dir=stems_root)
    (bundle_dir / "vocals.wav").unlink()  # a REAL part gone, in the post-load window

    outcome = library_verdicts._classify_loaded_bundle("sid-vanish-scan-001", None, bundle)

    assert outcome.attempt is None
    assert outcome.skip_reason is None
    assert outcome.fail_reason is not None
    assert "vocals.wav" in outcome.fail_reason


@pytest.mark.skipif(
    _RUNNING_AS_ROOT,
    reason="root bypasses filesystem permission checks; mode 000 cannot "
    "produce a real permission failure under root",
)
def test_classify_loaded_bundle_reports_an_unreadable_bundle_dir_as_a_failure(
    data_dir,
) -> None:
    """Sibling real fixture: the bundle DIRECTORY made unreadable (mode 000)
    after a successful load blocks the second stat() pass with a genuine,
    kernel-enforced ``PermissionError`` -- a different real-fault flavor
    from a removed part, same code path."""
    stems_root = _stems_root(data_dir)
    bundle_dir = _write_wav_bundle(stems_root, "sid-unreadable-scan-001", source_sha256="2" * 64)
    bundle = load_stem_bundle("sid-unreadable-scan-001", stems_dir=stems_root)
    bundle_dir.chmod(0o000)
    try:
        outcome = library_verdicts._classify_loaded_bundle(
            "sid-unreadable-scan-001", None, bundle
        )
    finally:
        # Restore so pytest's tmp_path teardown can remove it.
        bundle_dir.chmod(0o755)

    assert outcome.fail_reason is not None
    assert outcome.attempt is None
    assert outcome.skip_reason is None


def test_cached_coverage_pct_raises_when_a_part_vanishes_after_load(data_dir) -> None:
    """Same real fault, the SECOND unguarded call site (Sol review round 6):
    ``_cached_coverage_pct``'s bundle-identity comparison re-checks
    ``bundle_stem_identity`` in the WRITE phase, independently of the scan
    phase's own check. A part removed after load must raise ``RuntimeError``
    here (matching this function's existing pattern for its OTHER
    filesystem read, the vocal-cache file itself) so ``backfill_verdicts``'s
    write loop -- which already catches ``RuntimeError`` -- turns it into a
    per-track FAILURE instead of an unhandled ``OSError`` aborting the run."""
    stems_root = _stems_root(data_dir)
    bundle_dir = _write_wav_bundle(stems_root, "sid-vanish-cache-001", source_sha256="3" * 64)
    bundle = load_stem_bundle("sid-vanish-cache-001", stems_dir=stems_root)
    _seed_matching_cache(data_dir, "sid-vanish-cache-001", bundle, coverage_pct=42.0)
    (bundle_dir / "drums.wav").unlink()  # a REAL part gone, after both the load AND the cache seed

    with pytest.raises(RuntimeError, match="could not be re-checked"):
        library_verdicts._cached_coverage_pct(data_dir, "sid-vanish-cache-001", bundle)


def test_write_candidate_reports_a_vanished_part_as_a_failure_not_an_abort(
    conn, data_dir
) -> None:
    """End-to-end proof for the write-phase seam ``backfill_verdicts`` calls
    internally: given a candidate whose bundle was scanned and cached
    successfully, then a real part vanishes before the write step runs,
    ``_write_candidate`` must return ``_WriteOutcome.fail_reason`` -- never
    raise -- so the caller's loop can keep going to the next candidate."""
    seed_track(conn, "sid-vanish-write-001")
    stems_root = _stems_root(data_dir)
    bundle_dir = _write_wav_bundle(stems_root, "sid-vanish-write-001", source_sha256="4" * 64)
    bundle = load_stem_bundle("sid-vanish-write-001", stems_dir=stems_root)
    expected_source = library_verdicts._bundle_source_label(bundle)  # the SCAN phase's own call
    _seed_matching_cache(data_dir, "sid-vanish-write-001", bundle, coverage_pct=17.0)
    (bundle_dir / "bass.wav").unlink()  # real fault, landing AFTER the scan already succeeded

    outcome = library_verdicts._write_candidate(
        conn, data_dir, "sid-vanish-write-001", bundle, expected_source
    )

    assert outcome.fail_reason is not None
    assert outcome.race_lost is False
    assert store.get_verdict(conn, "sid-vanish-write-001") is None


def test_backfill_continues_past_one_candidates_real_filesystem_fault(
    conn, data_dir
) -> None:
    """Full-orchestration proof that a real post-load filesystem fault on
    ONE candidate is reported as a FAILURE by name while a SIBLING candidate
    is computed normally in the SAME run -- driven through
    ``backfill_verdicts``'s own two composable phases (``_scan_candidate``
    then ``_write_candidate``), in the same shape ``backfill_verdicts``
    itself uses them, because a single synchronous call to
    ``backfill_verdicts`` cannot be paused mid-flight to inject the real
    mutation exactly between its internal scan and write phases without a
    timing race. Every decision below (skip/fail/attempt, written/race/fail)
    is made by the SAME production functions ``backfill_verdicts`` calls;
    nothing here reimplements its logic."""
    seed_track(conn, "sid-vanish-sibling-victim-001")
    seed_track(conn, "sid-vanish-sibling-ok-001")
    stems_root = _stems_root(data_dir)
    demucs_root, roformer_root = library_verdicts._bundle_roots(data_dir)

    victim_dir = _write_wav_bundle(
        stems_root, "sid-vanish-sibling-victim-001", source_sha256="5" * 64
    )
    _write_wav_bundle(stems_root, "sid-vanish-sibling-ok-001", source_sha256="6" * 64)

    # Scan phase: both candidates load and classify successfully for real,
    # exactly as backfill_verdicts's own scan loop would do.
    victim_outcome = library_verdicts._scan_candidate(
        conn,
        "sid-vanish-sibling-victim-001",
        reserved=frozenset(),
        demucs_root=demucs_root,
        roformer_root=roformer_root,
    )
    ok_outcome = library_verdicts._scan_candidate(
        conn,
        "sid-vanish-sibling-ok-001",
        reserved=frozenset(),
        demucs_root=demucs_root,
        roformer_root=roformer_root,
    )
    assert victim_outcome.attempt is not None
    assert ok_outcome.attempt is not None
    # Both candidates get a matching vocal-cache entry BEFORE the mutation,
    # exactly as a real prior ``from-stems`` run would have left one -- this
    # is what routes the write phase through the vulnerable bundle-identity
    # comparison instead of a real (soundfile-dependent) decode.
    _seed_matching_cache(
        data_dir, "sid-vanish-sibling-victim-001", victim_outcome.attempt[1], coverage_pct=71.0
    )
    _seed_matching_cache(
        data_dir, "sid-vanish-sibling-ok-001", ok_outcome.attempt[1], coverage_pct=63.0
    )

    # The real fault: the victim's part vanishes AFTER its scan already
    # succeeded, before the write phase reaches it.
    (victim_dir / "other.wav").unlink()

    # Write phase: same call backfill_verdicts's own write loop makes, for
    # each scanned candidate in turn.
    victim_write = library_verdicts._write_candidate(
        conn, data_dir, *victim_outcome.attempt
    )
    ok_write = library_verdicts._write_candidate(conn, data_dir, *ok_outcome.attempt)

    assert victim_write.fail_reason is not None, "the victim must be a FAILURE, not silent"
    assert ok_write.fail_reason is None and ok_write.race_lost is False, (
        "the untouched sibling must still be computed normally in the same run"
    )
    assert store.get_verdict(conn, "sid-vanish-sibling-victim-001") is None
    ok_verdict = store.get_verdict(conn, "sid-vanish-sibling-ok-001")
    assert ok_verdict is not None
    assert ok_verdict.coverage_pct == 63.0


#-----------------------------------------------------------------------------
# vocal-cache identity guard: fast, stdlib-only (no ffmpeg / no soundfile),
# so these run in every environment including this repo's dev-only venv.
#-----------------------------------------------------------------------------
def test_cached_coverage_pct_reuses_a_matching_bundle_identity(data_dir) -> None:
    """A cache entry stamped with THIS bundle's own layout + source hash is
    the shape the coverage-reuse fast path is allowed to serve."""
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-cache-match-001", source_sha256="1" * 64)
    bundle = load_stem_bundle("sid-cache-match-001", stems_dir=stems_root)
    cache_path = vcache.cache_path(data_dir, "sid-cache-match-001")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema": vcache.VOCAL_CACHE_SCHEMA,
                "coverage_pct": 42.0,
                "duration_s": library_verdicts._bundle_duration_s(bundle),
                "params": {
                    "derived_from_stems": True,
                    "bundle_layout": bundle.layout,
                    "bundle_stem_sha256": vfrom_stems.bundle_stem_identity(bundle),
                },
            }
        ),
        encoding="utf-8",
    )

    cached = library_verdicts._cached_coverage_pct(data_dir, "sid-cache-match-001", bundle)

    assert cached == 42.0


def test_cached_coverage_pct_rejects_a_replaced_bundle(data_dir) -> None:
    """P1 BLOCKING fix (Sol review, PR #2611, library_verdicts.py:189): a
    cache entry stamped for a DIFFERENT bundle generation, or carrying no
    identity at all (the shape every entry on disk has today, before this
    fix), must never be served -- even when its recorded duration happens
    to match the on-disk bundle."""
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-cache-mismatch-001", source_sha256="2" * 64)
    bundle = load_stem_bundle("sid-cache-mismatch-001", stems_dir=stems_root)
    duration_s = library_verdicts._bundle_duration_s(bundle)
    cache_path = vcache.cache_path(data_dir, "sid-cache-mismatch-001")
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    # Case 1: identity fields present, but for a DIFFERENT stem generation --
    # the exact "same duration, replaced/re-separated bundle" scenario the
    # review names. A real prior identity is 64 hex chars, just not THIS one.
    cache_path.write_text(
        json.dumps(
            {
                "schema": vcache.VOCAL_CACHE_SCHEMA,
                "coverage_pct": 77.0,
                "duration_s": duration_s,
                "params": {
                    "derived_from_stems": True,
                    "bundle_layout": bundle.layout,
                    "bundle_stem_sha256": "1" * 64,  # a PRIOR generation's identity
                },
            }
        ),
        encoding="utf-8",
    )
    assert (
        library_verdicts._cached_coverage_pct(data_dir, "sid-cache-mismatch-001", bundle)
        is None
    )

    # Case 2: legacy entry, no identity fields at all -- duration matches,
    # nothing else does.
    cache_path.write_text(
        json.dumps(
            {
                "schema": vcache.VOCAL_CACHE_SCHEMA,
                "coverage_pct": 77.0,
                "duration_s": duration_s,
                "params": {"derived_from_stems": True},
            }
        ),
        encoding="utf-8",
    )
    assert (
        library_verdicts._cached_coverage_pct(data_dir, "sid-cache-mismatch-001", bundle)
        is None
    )


def test_cached_coverage_pct_rejects_non_finite_and_out_of_range_values(data_dir) -> None:
    """P1 BLOCKING fix (Sol follow-up review, PR #2611, library_verdicts.py:210):
    ``json.loads`` happily parses ``NaN``/``Infinity`` (valid JS, invalid
    JSON) as Python floats that pass a bare ``isinstance(x, (int, float))``
    check, and a NaN ``duration_s`` would then pass the tolerance comparison
    too, since every comparison against NaN is False. A corrupt or malicious
    cache entry must fall through to a real derive, exactly like a missing
    or unreadable one -- never render as a trusted coverage number."""
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-cache-nonfinite-001", source_sha256="6" * 64)
    bundle = load_stem_bundle("sid-cache-nonfinite-001", stems_dir=stems_root)
    identity = vfrom_stems.bundle_stem_identity(bundle)
    cache_path = vcache.cache_path(data_dir, "sid-cache-nonfinite-001")
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    def _entry(coverage_pct, duration_s) -> str:
        # json.dumps refuses NaN/Infinity by default -- the malformed cache
        # entries this test defends against are written the same way a
        # corrupt or malicious producer would: hand-built JSON text, not
        # Python's own serializer objecting on their behalf.
        return (
            f'{{"schema": {vcache.VOCAL_CACHE_SCHEMA}, "coverage_pct": {coverage_pct}, '
            f'"duration_s": {duration_s}, "params": {{"derived_from_stems": true, '
            f'"bundle_layout": "{bundle.layout}", "bundle_stem_sha256": "{identity}"}}}}'
        )

    real_duration = library_verdicts._bundle_duration_s(bundle)

    # NaN coverage_pct (duration otherwise valid and matching).
    cache_path.write_text(_entry("NaN", real_duration), encoding="utf-8")
    assert (
        library_verdicts._cached_coverage_pct(data_dir, "sid-cache-nonfinite-001", bundle)
        is None
    )

    # Infinity duration_s (coverage_pct otherwise valid).
    cache_path.write_text(_entry(10.0, "Infinity"), encoding="utf-8")
    assert (
        library_verdicts._cached_coverage_pct(data_dir, "sid-cache-nonfinite-001", bundle)
        is None
    )

    # -Infinity duration_s too.
    cache_path.write_text(_entry(10.0, "-Infinity"), encoding="utf-8")
    assert (
        library_verdicts._cached_coverage_pct(data_dir, "sid-cache-nonfinite-001", bundle)
        is None
    )

    # coverage_pct out of the valid [0, 100] range, otherwise well-formed.
    cache_path.write_text(_entry(150.0, real_duration), encoding="utf-8")
    assert (
        library_verdicts._cached_coverage_pct(data_dir, "sid-cache-nonfinite-001", bundle)
        is None
    )

    # A negative coverage_pct is equally out of range.
    cache_path.write_text(_entry(-1.0, real_duration), encoding="utf-8")
    assert (
        library_verdicts._cached_coverage_pct(data_dir, "sid-cache-nonfinite-001", bundle)
        is None
    )

    # duration_s <= 0 is nonsensical for a real bundle.
    cache_path.write_text(_entry(10.0, 0), encoding="utf-8")
    assert (
        library_verdicts._cached_coverage_pct(data_dir, "sid-cache-nonfinite-001", bundle)
        is None
    )

    # Opposite-direction control: a fully valid, finite, in-range entry
    # (identical shape to the rejected ones above, minus the defect) is
    # still reused.
    cache_path.write_text(_entry(10.0, real_duration), encoding="utf-8")
    assert (
        library_verdicts._cached_coverage_pct(data_dir, "sid-cache-nonfinite-001", bundle)
        == 10.0
    )


def test_cached_coverage_pct_raises_on_a_present_but_corrupt_cache_entry(data_dir) -> None:
    """P1 BLOCKING fix (Sol review round 4, PR #2611, library_verdicts.py:227):
    a MISSING cache file and a PRESENT-but-unreadable one were folded into
    the same ``None`` return, so a corrupt cache file was silently treated
    as "no cache" and the caller derived fresh coverage and reported the
    track as successfully processed -- masking a real filesystem/corruption
    fault from the operator, against the project's fail-fast rule. A
    present file that fails to parse as JSON must raise, not return None."""
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-cache-corrupt-001", source_sha256="b" * 64)
    bundle = load_stem_bundle("sid-cache-corrupt-001", stems_dir=stems_root)
    cache_path = vcache.cache_path(data_dir, "sid-cache-corrupt-001")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{this is not valid json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        library_verdicts._cached_coverage_pct(data_dir, "sid-cache-corrupt-001", bundle)


@pytest.mark.skipif(
    _RUNNING_AS_ROOT,
    reason="root bypasses filesystem permission checks; mode 000 cannot "
    "produce a real permission failure under root",
)
def test_cached_coverage_pct_raises_on_a_permission_denied_cache_file(data_dir) -> None:
    """P1 BLOCKING fix (Sol review round 5, PR #2611): the round-4 test for
    this code path monkeypatched ``Path.read_text`` to synthesize an
    ``OSError``, which this project's no-mocks test contract forbids -- the
    unreadable-cache behavior must be validated against a REAL production
    failure, not a simulated one. ``os.chmod(0o000)`` on a real file this
    (non-root) process then genuinely cannot read produces that failure for
    real: the kernel itself refuses the read, raising a real ``OSError``."""
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-cache-denied-001", source_sha256="c" * 64)
    bundle = load_stem_bundle("sid-cache-denied-001", stems_dir=stems_root)
    cache_path = vcache.cache_path(data_dir, "sid-cache-denied-001")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{}", encoding="utf-8")
    cache_path.chmod(0o000)
    try:
        with pytest.raises(RuntimeError, match="could not be read"):
            library_verdicts._cached_coverage_pct(data_dir, "sid-cache-denied-001", bundle)
    finally:
        # Restore read/write so pytest's tmp_path teardown can remove it.
        cache_path.chmod(0o644)


def test_cached_coverage_pct_raises_when_a_directory_sits_at_the_cache_path(
    data_dir,
) -> None:
    """Sibling real fixture for the same code path (Sol review round 5,
    PR #2611): a DIRECTORY where a cache file is expected also produces a
    genuine, unmocked ``OSError`` (``IsADirectoryError``) when actually
    read -- distinct from a genuinely MISSING path, which is why the
    "nothing here" guard checks :meth:`Path.exists` rather than
    :meth:`Path.is_file` (see ``_cached_coverage_pct``'s docstring)."""
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-cache-isdir-001", source_sha256="0" * 64)
    bundle = load_stem_bundle("sid-cache-isdir-001", stems_dir=stems_root)
    cache_path = vcache.cache_path(data_dir, "sid-cache-isdir-001")
    cache_path.mkdir(parents=True, exist_ok=True)  # a REAL directory, not a file

    with pytest.raises(RuntimeError, match="could not be read"):
        library_verdicts._cached_coverage_pct(data_dir, "sid-cache-isdir-001", bundle)


def test_backfill_reports_a_corrupt_cache_entry_as_a_failure_not_a_miss(
    conn, data_dir
) -> None:
    """End-to-end control, proving the PRESENCE of the good thing per the
    project's verification rule: the backfill itself must surface a
    present-but-corrupt cache entry as a FAILURE line an operator can see,
    never silently re-derive and report the track as successfully
    processed."""
    seed_track(conn, "sid-cache-corrupt-backfill-001")
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-cache-corrupt-backfill-001", source_sha256="d" * 64)
    cache_path = vcache.cache_path(data_dir, "sid-cache-corrupt-backfill-001")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{this is not valid json", encoding="utf-8")

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ()
    assert "sid-cache-corrupt-backfill-001" in report.failed
    assert "not valid JSON" in report.failed["sid-cache-corrupt-backfill-001"]
    assert store.get_verdict(conn, "sid-cache-corrupt-backfill-001") is None


@pytest.mark.skipif(
    _RUNNING_AS_ROOT,
    reason="root bypasses filesystem permission checks; mode 000 cannot "
    "produce a real permission failure under root",
)
def test_backfill_reports_a_permission_denied_cache_file_as_a_failure(
    conn, data_dir
) -> None:
    """End-to-end control for the permission-denied unit test above: the
    backfill itself must surface it as a FAILURE, not a silent re-derive."""
    seed_track(conn, "sid-cache-denied-backfill-001")
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-cache-denied-backfill-001", source_sha256="1" * 64)
    cache_path = vcache.cache_path(data_dir, "sid-cache-denied-backfill-001")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{}", encoding="utf-8")
    cache_path.chmod(0o000)
    try:
        report = library_verdicts.backfill_verdicts(
            conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
        )
    finally:
        cache_path.chmod(0o644)

    assert report.processed == ()
    assert "sid-cache-denied-backfill-001" in report.failed
    assert "could not be read" in report.failed["sid-cache-denied-backfill-001"]
    assert store.get_verdict(conn, "sid-cache-denied-backfill-001") is None


def test_backfill_reports_a_directory_at_the_cache_path_as_a_failure(
    conn, data_dir
) -> None:
    """End-to-end control for the directory-at-path unit test above: the
    backfill itself must surface it as a FAILURE, not a silent re-derive."""
    seed_track(conn, "sid-cache-isdir-backfill-001")
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-cache-isdir-backfill-001", source_sha256="2" * 64)
    cache_path = vcache.cache_path(data_dir, "sid-cache-isdir-backfill-001")
    cache_path.mkdir(parents=True, exist_ok=True)

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ()
    assert "sid-cache-isdir-backfill-001" in report.failed
    assert "could not be read" in report.failed["sid-cache-isdir-backfill-001"]
    assert store.get_verdict(conn, "sid-cache-isdir-backfill-001") is None


def test_backfill_reuses_a_readable_cache_entry_as_the_opposite_direction_control(
    conn, data_dir
) -> None:
    """Opposite-direction control for the two tests above: a present,
    READABLE, compatible cache entry must still be reused and processed
    normally -- the fix must not turn every cache hit into a failure."""
    seed_track(conn, "sid-cache-readable-001")
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-cache-readable-001", source_sha256="e" * 64)
    bundle = load_stem_bundle("sid-cache-readable-001", stems_dir=stems_root)
    cache_path = vcache.cache_path(data_dir, "sid-cache-readable-001")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema": vcache.VOCAL_CACHE_SCHEMA,
                "coverage_pct": 5.0,
                "duration_s": library_verdicts._bundle_duration_s(bundle),
                "params": {
                    "derived_from_stems": True,
                    "bundle_layout": bundle.layout,
                    "bundle_stem_sha256": vfrom_stems.bundle_stem_identity(bundle),
                },
            }
        ),
        encoding="utf-8",
    )

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.failed == {}
    assert report.processed == ("sid-cache-readable-001",)
    assert report.reused_cache == ("sid-cache-readable-001",)
    verdict = store.get_verdict(conn, "sid-cache-readable-001")
    assert verdict is not None
    assert verdict.coverage_pct == 5.0


@requires_ffmpeg
@requires_mp3_loader
@pytest.mark.requires_audio_stack
def test_stale_vocal_cache_from_a_replaced_bundle_is_not_reused(conn, data_dir) -> None:
    """A vocal-cache entry computed for a PRIOR bundle generation (same
    on-disk duration, no bundle-identity link) must never be served for a
    REPLACED bundle -- see apps/lyrics/library_verdicts.py:_cached_coverage_pct.

    Sol review, PR #2611, apps/lyrics/library_verdicts.py:189: "A cache entry
    is accepted solely by stable ID and duration, so replacing a bundle with
    a new source.sha256 but the same duration reuses coverage calculated
    from the old stems."
    """
    seed_track(conn, "sid-cache-stale-001")
    stems_root = _stems_root(data_dir)

    # Generation 1: loud vocal, real high coverage. Simulates a vocal-cache
    # entry a prior ``python -m apps.vocals from-stems`` run left on disk --
    # legacy shape, no bundle-identity fields at all.
    _write_bundle(stems_root, "sid-cache-stale-001", vocal_loud=True, source_sha256="1" * 64)
    cache_path = vcache.cache_path(data_dir, "sid-cache-stale-001")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema": vcache.VOCAL_CACHE_SCHEMA,
                "coverage_pct": 90.0,
                "duration_s": DURATION_S,
                "params": {"derived_from_stems": True},
            }
        ),
        encoding="utf-8",
    )

    # Generation 2 REPLACES it before this backfill ever runs: same duration,
    # opposite (silent) vocal content, a new source hash.
    shutil.rmtree(stems_root / "sid-cache-stale-001")
    _write_bundle(stems_root, "sid-cache-stale-001", vocal_loud=False, source_sha256="2" * 64)

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ("sid-cache-stale-001",)
    assert "sid-cache-stale-001" not in report.reused_cache, (
        "cache entry carries no bundle identity -- must recompute, not reuse"
    )
    verdict = store.get_verdict(conn, "sid-cache-stale-001")
    assert verdict is not None
    assert verdict.verdict == "no-lyrics", "stale generation-1 cache must not mask generation 2"


#-----------------------------------------------------------------------------
# backfill vs. a concurrent word-data write: fast, stdlib-only (a WAV bundle
# plus a matching vocal-cache entry means no ffmpeg/soundfile decode either).
#-----------------------------------------------------------------------------
def test_backfill_never_clobbers_a_word_row_written_during_the_race(
    conn, data_dir
) -> None:
    """P1 BLOCKING fix (Devin review, PR #2611, library_verdicts.py:304),
    strengthened by a Sol follow-up review at 8d2dc8ddb9, then REWRITTEN for
    a Sol round-6 review (PR #2611): the earlier version of this test drove
    the race by ``monkeypatch.setattr``-ing ``coverage_pct_for_bundle``,
    fabricating the scheduling point instead of exercising a genuine
    concurrent writer -- against this project's no-mocks test contract. This
    version drives the SAME race with REAL SQLite concurrency: the scan
    phase runs for real and captures its candidate, a SEPARATE
    ``sqlite3`` connection (:func:`apps.shared.state.db.open_rw` on the SAME
    on-disk db file -- a genuinely different connection, not a second handle
    faked in-process) commits a real word-level row, and ONLY THEN does the
    write step run with that now-stale candidate -- the exact shape
    ``backfill_verdicts``'s own scan-then-write loop uses, just driven one
    candidate at a time so the concurrent write can land for real in
    between.

    The first fix (``COALESCE``-preserving n_words/n_lines/words_content_hash
    inside the SHARED ``upsert_verdict``) protected only those three columns
    -- the Sol follow-up pointed out that verdict/coverage_pct/source/
    language_iso3/pct_witness_red/pipeline_version/computed_at were all still
    silently overwritten with stem-only values. This test asserts the row
    that landed mid-race survives WHOLLY intact, every column, and that the
    write step reports the loss as a race
    (:attr:`library_verdicts._WriteOutcome.race_lost`), never a silent
    success."""
    seed_track(conn, "sid-race-001")
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-race-001", source_sha256="9" * 64)
    bundle = load_stem_bundle("sid-race-001", stems_dir=stems_root)
    _seed_matching_cache(data_dir, "sid-race-001", bundle, coverage_pct=5.0)

    # Scan phase: a real, unmocked candidate capture -- the row has no
    # verdict at all yet, so the scan schedules a fresh compute.
    demucs_root, roformer_root = library_verdicts._bundle_roots(data_dir)
    scan_outcome = library_verdicts._scan_candidate(
        conn,
        "sid-race-001",
        reserved=frozenset(),
        demucs_root=demucs_root,
        roformer_root=roformer_root,
    )
    assert scan_outcome.attempt is not None, "setup bug: scan must find a fresh candidate"

    # REAL concurrent writer: a genuinely separate connection to the SAME
    # on-disk db file, committing a real word-level row for this stable_id
    # -- not a function call intercepted in-process. Every field a
    # coverage-only write could otherwise touch gets a distinctive,
    # non-default value so the assertions below can catch ANY of them
    # getting overwritten.
    second_conn = state_db.open_rw(data_dir / "state" / "state.db", apply_schema=False)
    try:
        store.upsert_verdict(
            second_conn,
            stable_id="sid-race-001",
            verdict="vocal",
            coverage_pct=91.0,
            source="asr-real-run",
            language_iso3="eng",
            n_words=42,
            n_lines=6,
            pct_witness_red=12.5,
            pipeline_version="asr-v9",
            words_content_hash="a" * 64,
            computed_at="2026-09-14T00:00:00Z",
            resurrect=False,
        )
    finally:
        second_conn.close()

    # Write step, driven with the STALE candidate captured BEFORE the
    # concurrent write landed -- the same call backfill_verdicts's own
    # write loop makes for each scanned candidate.
    write_outcome = library_verdicts._write_candidate(conn, data_dir, *scan_outcome.attempt)

    assert write_outcome.race_lost is True, (
        "a race loss must be detected, not silently written over"
    )
    assert write_outcome.fail_reason is None
    verdict = store.get_verdict(conn, "sid-race-001")
    assert verdict is not None
    # EVERY field of the word-level row must survive untouched -- not just
    # the three word columns a narrower, earlier fix protected.
    assert verdict.verdict == "vocal"
    assert verdict.coverage_pct == 91.0
    assert verdict.source == "asr-real-run"
    assert verdict.language_iso3 == "eng"
    assert verdict.n_words == 42
    assert verdict.n_lines == 6
    assert verdict.pct_witness_red == 12.5
    assert verdict.pipeline_version == "asr-v9"
    assert verdict.words_content_hash == "a" * 64
    assert verdict.computed_at == "2026-09-14T00:00:00Z"


def test_backfill_never_clobbers_a_row_overridden_during_the_race(conn, data_dir) -> None:
    """Sibling of the word-level race test above, same defect class (Sol
    review round 4, PR #2611, library_verdicts.py:308), REWRITTEN the same
    way for the Sol round-6 review: a genuinely separate
    :func:`apps.shared.state.db.open_rw` connection on the SAME db file
    commits a real ``set_override`` call between a real scan capture and the
    write step, instead of a monkeypatched ``coverage_pct_for_bundle``."""
    seed_track(conn, "sid-override-race-001")
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-override-race-001", source_sha256="8" * 64)
    bundle = load_stem_bundle("sid-override-race-001", stems_dir=stems_root)
    # A matching vocal-cache entry means the write side's coverage lookup is
    # served from cache, not a real audio decode -- fast, stdlib-only, same
    # pattern as the word-level race test above.
    _seed_matching_cache(data_dir, "sid-override-race-001", bundle, coverage_pct=4.0)

    # A genuine PRE-EXISTING coverage-only row, stamped stale enough (an old
    # pipeline_version/source pair) that the scan schedules a real recompute
    # rather than skipping it as already up to date. set_override requires
    # an existing computed row, so this is also the setup for that.
    store.upsert_stem_coverage_verdict(
        conn,
        stable_id="sid-override-race-001",
        verdict="no-lyrics",
        coverage_pct=3.0,
        source="stem-coverage:v1:" + "0" * 64,
        pipeline_version="stale-pre-race-version",
        computed_at="2026-09-01T00:00:00.000000+00:00",
    )
    before = store.get_verdict(conn, "sid-override-race-001")
    assert before is not None and before.override is None

    demucs_root, roformer_root = library_verdicts._bundle_roots(data_dir)
    scan_outcome = library_verdicts._scan_candidate(
        conn,
        "sid-override-race-001",
        reserved=frozenset(),
        demucs_root=demucs_root,
        roformer_root=roformer_root,
    )
    assert scan_outcome.attempt is not None, "setup bug: scan must find a stale-but-live candidate"

    # REAL concurrent human override, via a genuinely separate connection to
    # the SAME on-disk db file, landing between the scan capture above and
    # the write step below.
    second_conn = state_db.open_rw(data_dir / "state" / "state.db", apply_schema=False)
    try:
        store.set_override(
            second_conn,
            stable_id="sid-override-race-001",
            override="vocal",
            note="race override",
        )
    finally:
        second_conn.close()

    write_outcome = library_verdicts._write_candidate(conn, data_dir, *scan_outcome.attempt)

    assert write_outcome.race_lost is True, (
        "a race loss must be detected, not silently written over"
    )
    assert write_outcome.fail_reason is None
    verdict = store.get_verdict(conn, "sid-override-race-001")
    assert verdict is not None
    # The computed fields from BEFORE the race must survive untouched --
    # this write must never overwrite them just because the override itself
    # would separately survive via the ON CONFLICT column omission.
    assert verdict.verdict == before.verdict
    assert verdict.coverage_pct == before.coverage_pct
    assert verdict.source == before.source
    assert verdict.pipeline_version == before.pipeline_version
    assert verdict.computed_at == before.computed_at
    assert verdict.override == "vocal"
    assert verdict.override_note == "race override"
    assert verdict.effective == "vocal"


def test_backfill_updates_a_coverage_only_row(conn, data_dir) -> None:
    """Opposite-direction control for the test above: a row that has NOT
    gone word-level (coverage-only, or absent entirely) IS updated by the
    backfill. ``upsert_stem_coverage_verdict``'s atomic guard only ever
    blocks a row that already carries ``words_content_hash`` -- it must
    never become a guard against writing at all."""
    seed_track(conn, "sid-coverage-update-001")
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-coverage-update-001", source_sha256="7" * 64)
    bundle = load_stem_bundle("sid-coverage-update-001", stems_dir=stems_root)
    cache_path = vcache.cache_path(data_dir, "sid-coverage-update-001")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema": vcache.VOCAL_CACHE_SCHEMA,
                "coverage_pct": 3.0,
                "duration_s": library_verdicts._bundle_duration_s(bundle),
                "params": {
                    "derived_from_stems": True,
                    "bundle_layout": bundle.layout,
                    "bundle_stem_sha256": vfrom_stems.bundle_stem_identity(bundle),
                },
            }
        ),
        encoding="utf-8",
    )

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ("sid-coverage-update-001",)
    assert library_verdicts.SKIP_PROTECTED_ROW_RACE not in report.skipped
    verdict = store.get_verdict(conn, "sid-coverage-update-001")
    assert verdict is not None
    assert verdict.coverage_pct == 3.0
    assert verdict.verdict == "no-lyrics"
    assert verdict.words_content_hash is None


@requires_ffmpeg
@requires_mp3_loader
def test_dry_run_writes_nothing(conn, data_dir) -> None:
    seed_track(conn, "sid-dryrun-001")
    _write_bundle(_stems_root(data_dir), "sid-dryrun-001", vocal_loud=True)

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=True, limit=None, include_reserved=False
    )

    assert report.processed == ("sid-dryrun-001",)
    assert store.get_verdict(conn, "sid-dryrun-001") is None


@requires_ffmpeg
@requires_mp3_loader
@pytest.mark.requires_audio_stack
def test_limit_caps_the_computed_set_not_the_skips(conn, data_dir) -> None:
    seed_track(conn, "sid-limit-a")
    seed_track(conn, "sid-limit-b")
    seed_track(conn, "sid-limit-reserved")
    _write_bundle(_stems_root(data_dir), "sid-limit-a", vocal_loud=True)
    _write_bundle(_stems_root(data_dir), "sid-limit-b", vocal_loud=True)
    _write_bundle(_stems_root(data_dir), "sid-limit-reserved", vocal_loud=True)
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / library_verdicts.RESERVED_FILENAME).write_text(
        json.dumps({"tracks": [{"stable_id": "sid-limit-reserved"}]}), encoding="utf-8"
    )

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=1, include_reserved=False
    )

    assert len(report.processed) == 1
    assert report.skipped.get(library_verdicts.SKIP_RESERVED) == ("sid-limit-reserved",)
    assert report.candidates == 3


def test_reserved_ids_absent_file_reserves_nothing(tmp_path: Path) -> None:
    """A fresh install has no reserved-100 file (it is a QA artifact written
    by hand) and ``include_reserved`` defaults to False, so absence must read
    as "nothing reserved", the same policy ADR-0024's hydration loader has
    for the same file. Round 7 briefly raised here, which would have refused
    every default backfill on every machine but the QA one. Built straight
    from ``tmp_path``, not the file-level ``data_dir`` fixture (which stubs a
    reserved file), so the file is genuinely absent."""
    assert library_verdicts.load_reserved_ids(tmp_path / "data") == frozenset()


def test_reserved_ids_policy_matches_the_hydration_loader(tmp_path: Path) -> None:
    """One file, two readers: they must agree on absent AND on present."""
    from apps.cloud import stem_hydration

    data_dir = tmp_path / "data"
    assert library_verdicts.load_reserved_ids(data_dir) == stem_hydration.load_reserved_ids(
        data_dir
    )
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "state" / library_verdicts.RESERVED_FILENAME).write_text(
        '{"tracks": [{"stable_id": "a"}, {"stable_id": "b"}]}', encoding="utf-8"
    )
    assert library_verdicts.load_reserved_ids(data_dir) == frozenset({"a", "b"})
    assert stem_hydration.load_reserved_ids(data_dir) == frozenset({"a", "b"})


def test_reserved_ids_raises_when_path_is_a_directory(tmp_path: Path) -> None:
    """``Path.is_file()`` (the pre-fix check) reads False for a directory
    sitting where the reserved file is expected too, silently folding that
    anomaly into the same "unset" path this fix closes -- the identical
    pattern this module already rejects for the vocal-cache read in
    ``_cached_coverage_pct``."""
    a_data_dir = tmp_path / "data"
    (a_data_dir / "state" / library_verdicts.RESERVED_FILENAME).mkdir(parents=True)
    with pytest.raises(library_verdicts.ReservedIdsError):
        library_verdicts.load_reserved_ids(a_data_dir)


def test_reserved_ids_raises_on_malformed_json(tmp_path: Path) -> None:
    a_data_dir = tmp_path / "data"
    reserved = a_data_dir / "state" / library_verdicts.RESERVED_FILENAME
    reserved.parent.mkdir(parents=True)
    reserved.write_text("not valid json", encoding="utf-8")
    with pytest.raises(library_verdicts.ReservedIdsError):
        library_verdicts.load_reserved_ids(a_data_dir)


def test_reserved_ids_loads_ids_from_a_present_file(tmp_path: Path) -> None:
    """Opposite-direction control for the three tests above: a genuinely
    PRESENT, well-formed reserved file still loads its ids -- the fail-
    closed fix must not overshoot into refusing every reserved file, only a
    missing/unreadable/malformed one."""
    a_data_dir = tmp_path / "data"
    reserved = a_data_dir / "state" / library_verdicts.RESERVED_FILENAME
    reserved.parent.mkdir(parents=True)
    reserved.write_text(
        json.dumps({"tracks": [{"stable_id": "sid-a"}, {"stable_id": "sid-b"}]}),
        encoding="utf-8",
    )
    assert library_verdicts.load_reserved_ids(a_data_dir) == frozenset({"sid-a", "sid-b"})


def test_negative_limit_is_rejected(conn, data_dir) -> None:
    with pytest.raises(ValueError, match="limit"):
        library_verdicts.backfill_verdicts(
            conn, data_dir=data_dir, dry_run=False, limit=-1, include_reserved=False
        )
