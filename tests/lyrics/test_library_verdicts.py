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
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from apps.lyrics import library_verdicts, store
from apps.vocals import cache as vcache
from apps.webui.server.stem_artifacts import STEM_PARTS, StemArtifactError, load_stem_bundle

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
#: Tests that render real MP3 fixtures AND run them through the actual
#: coverage decode (``derive_worker_result``) need all three: ffmpeg to
#: render the fixture, PR #2593 to load it, and the ``analysis`` extra
#: (soundfile/numpy) to decode it. Tests that only exercise a guard which
#: short-circuits BEFORE loading (reserved, has-words) or before decoding
#: (dry-run, corrupt bundle) need a strict subset -- applied individually per
#: test below so a missing ``analysis`` extra never hides an otherwise-
#: runnable guard test behind an unrelated skip.


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
def test_backfill_preserves_a_human_override(conn, data_dir) -> None:
    seed_track(conn, "sid-override-001")
    _write_bundle(_stems_root(data_dir), "sid-override-001", vocal_loud=False)
    library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )
    store.set_override(
        conn, stable_id="sid-override-001", override="vocal", note="I heard singing"
    )

    # A changed bundle forces a genuine recompute (new source hash), the
    # scenario that would clobber an override if the guarantee ever broke.
    shutil.rmtree(_stems_root(data_dir) / "sid-override-001")
    _write_bundle(
        _stems_root(data_dir), "sid-override-001", vocal_loud=False,
        source_sha256="c" * 64,
    )
    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ("sid-override-001",)
    verdict = store.get_verdict(conn, "sid-override-001")
    assert verdict is not None
    assert verdict.verdict == "no-lyrics", "the computed value still updates"
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
                    "bundle_source_sha256": bundle.manifest.source.sha256,
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

    # Case 1: identity fields present, but for a DIFFERENT source hash --
    # the exact "same duration, replaced bundle" scenario the review names.
    cache_path.write_text(
        json.dumps(
            {
                "schema": vcache.VOCAL_CACHE_SCHEMA,
                "coverage_pct": 77.0,
                "duration_s": duration_s,
                "params": {
                    "derived_from_stems": True,
                    "bundle_layout": bundle.layout,
                    "bundle_source_sha256": "1" * 64,  # a PRIOR generation's hash
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
    conn, data_dir, monkeypatch
) -> None:
    """P1 BLOCKING fix (Devin review, PR #2611, library_verdicts.py:304): the
    scan phase's ``store.get_verdict`` check for SKIP_HAS_WORDS is not atomic
    with the write phase's ``upsert_verdict`` call, so a real ASR/aligner
    write can land in the gap between them. This drives that exact gap: a
    concurrent writer sets word-level data on the row from inside
    ``coverage_pct_for_bundle`` (called immediately before this module's own
    ``upsert_verdict`` call, after the scan already decided not to skip), and
    the backfill's coverage-only write -- which always passes
    ``n_words=None, n_lines=None, words_content_hash=None`` -- must not NULL
    the word columns back out."""
    seed_track(conn, "sid-race-001")
    stems_root = _stems_root(data_dir)
    _write_wav_bundle(stems_root, "sid-race-001", source_sha256="9" * 64)
    bundle = load_stem_bundle("sid-race-001", stems_dir=stems_root)
    cache_path = vcache.cache_path(data_dir, "sid-race-001")
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
                    "bundle_source_sha256": bundle.manifest.source.sha256,
                },
            }
        ),
        encoding="utf-8",
    )

    real_coverage_pct_for_bundle = library_verdicts.coverage_pct_for_bundle

    def _racing_coverage_pct_for_bundle(data_dir_arg, stable_id, bundle_arg):
        if stable_id == "sid-race-001":
            # The real ASR/aligner pipeline writes its own row IN THE GAP
            # between the scan phase's get_verdict check (already passed,
            # since it found no row at all) and this module's own
            # upsert_verdict call, a few lines below this return.
            store.upsert_verdict(
                conn,
                stable_id="sid-race-001",
                verdict="vocal",
                coverage_pct=91.0,
                source="asr-real-run",
                language_iso3="eng",
                n_words=42,
                n_lines=6,
                pct_witness_red=None,
                pipeline_version="asr-v9",
                words_content_hash="a" * 64,
                computed_at="2026-09-14T00:00:00Z",
                resurrect=False,
            )
        return real_coverage_pct_for_bundle(data_dir_arg, stable_id, bundle_arg)

    monkeypatch.setattr(
        library_verdicts, "coverage_pct_for_bundle", _racing_coverage_pct_for_bundle
    )

    report = library_verdicts.backfill_verdicts(
        conn, data_dir=data_dir, dry_run=False, limit=None, include_reserved=False
    )

    assert report.processed == ("sid-race-001",)
    verdict = store.get_verdict(conn, "sid-race-001")
    assert verdict is not None
    assert verdict.n_words == 42
    assert verdict.n_lines == 6
    assert verdict.words_content_hash == "a" * 64, (
        "backfill's coverage-only write must never clobber word data that "
        "landed in the get_verdict-to-upsert_verdict race window"
    )
    # The coverage-only write still lands: source/coverage_pct are the
    # backfill's own values, only the word columns are protected.
    assert verdict.source == library_verdicts._bundle_source_label(bundle)


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


def test_reserved_ids_load_empty_set_when_file_absent(data_dir: Path) -> None:
    assert library_verdicts.load_reserved_ids(data_dir) == frozenset()


def test_negative_limit_is_rejected(conn, data_dir) -> None:
    with pytest.raises(ValueError, match="limit"):
        library_verdicts.backfill_verdicts(
            conn, data_dir=data_dir, dry_run=False, limit=-1, include_reserved=False
        )
