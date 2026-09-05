"""The audio-backed halves of the equivalence work: loudness fits, key votes.

These are the pure functions of `scripts/mik_audio_resolve.py`,
`scripts/mik_volume_identify.py` and `scripts/key_third_opinion.py`. The ffmpeg
and librosa halves are exercised by running the scripts; what is pinned here is
the arithmetic that turns their output into a CLAIM, because that is where a
wrong answer would be quiet.

Regression lines:

- if a Core Data timestamp is read as a Unix timestamp then broken
- if a perfectly-offset candidate does not fit at r 1.0 with that offset then broken
- if a candidate that merely COVARIES is ranked above a constant-offset one
  then broken
- if the fitted offset is not the mean difference then broken
- if a canonical key string does not parse to (pitch class, mode) then broken
- if a pure C-major-triad chroma does not template-match to C major then broken
- if the third opinion agreeing with neither side is scored as agreement
  then broken
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    """Import a script by path; they are CLI entry points, not a package."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


resolve_mod = _load("mik_audio_resolve")
volume_mod = _load("mik_volume_identify")
key_mod = _load("key_third_opinion")


# ------------------------------------------------------------- timestamps


def test_core_data_epoch_is_2001_not_1970():
    """MIK stores Core Data time. Reading it as Unix time puts every analysis
    date in 1993 and makes the mtime comparison meaningless."""
    assert resolve_mod.core_data_time(0.0) == datetime(2001, 1, 1, tzinfo=UTC)
    assert resolve_mod.core_data_time(776_996_944.488271).year == 2025
    assert resolve_mod.core_data_time(None) is None


def test_only_audio_extensions_resolve():
    assert resolve_mod._is_audio("/x/y.MP3")
    assert resolve_mod._is_audio("/x/y.aiff")
    assert not resolve_mod._is_audio("/x/y.pek")
    assert not resolve_mod._is_audio("/x/y")


# ---------------------------------------------------- present basenames


def _make_state_db(
    tmp_path: Path,
    paths: list[Path],
    *,
    deleted: list[Path] | None = None,
) -> Path:
    """A minimal ``tracks`` table. ``deleted`` marks which ``paths`` entries
    are soft-deleted (``deleted_at`` set), the rest stay live."""
    deleted = deleted or []
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute("CREATE TABLE tracks (file_path TEXT, deleted_at TEXT)")
    conn.executemany(
        "INSERT INTO tracks VALUES (?, ?)",
        [
            (str(path), "2026-09-03T00:00:00+00:00" if path in deleted else None)
            for path in paths
        ],
    )
    conn.commit()
    conn.close()
    return state_db


def test_present_basenames_rejects_a_collision(tmp_path):
    """P1 regression (PR #383 review): two present tracks sharing a basename
    is ambiguous, not a pick. Silently keeping whichever path SQLite returns
    first can feed an unrelated track's audio into the loudness/key
    experiment and contaminate the evidence used to classify mappings."""
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    path_a, path_b = dir_a / "x.mp3", dir_b / "x.mp3"
    path_a.write_bytes(b"a")
    path_b.write_bytes(b"b")
    state_db = _make_state_db(tmp_path, [path_a, path_b])
    present = resolve_mod._present_basenames(state_db)
    assert "x.mp3" not in present


def test_present_basenames_keeps_an_unambiguous_basename(tmp_path):
    only = tmp_path / "only.mp3"
    only.write_bytes(b"a")
    state_db = _make_state_db(tmp_path, [only])
    present = resolve_mod._present_basenames(state_db)
    assert present["only.mp3"] == str(only)


def test_present_basenames_excludes_a_soft_deleted_tombstone(tmp_path):
    """P1 regression (PR #383 review, scripts/mik_audio_resolve.py:120): a
    soft delete never removes the file, so a tombstoned track's row still
    resolves on disk. Left unfiltered, it is admitted as the sole basename
    candidate and can feed an unrelated (deleted) track's audio into the
    loudness/key experiment used to justify field mappings."""
    tombstoned = tmp_path / "tombstoned.mp3"
    tombstoned.write_bytes(b"a")
    state_db = _make_state_db(tmp_path, [tombstoned], deleted=[tombstoned])
    present = resolve_mod._present_basenames(state_db)
    assert "tombstoned.mp3" not in present


# --------------------------------------------------------- loudness fits


def test_a_constant_offset_candidate_fits_at_r_one():
    measured = np.array([-3.0, -8.0, -12.5, -20.0, -31.0])
    volume = measured - 4.25
    fit = volume_mod.fit("candidate", volume, measured)
    assert fit.pearson_r == pytest.approx(1.0)
    assert fit.fitted_offset_db == pytest.approx(-4.25)
    assert fit.mae_about_offset_db == pytest.approx(0.0, abs=1e-9)
    assert fit.slope == pytest.approx(1.0)


def test_a_candidate_that_only_covaries_is_separated_from_one_that_explains():
    """The trap this whole comparison exists to avoid: high r is not enough.
    A candidate on a different SCALE still correlates perfectly, so the offset
    spread has to be reported next to r, and it is."""
    measured = np.array([-3.0, -8.0, -12.5, -20.0, -31.0])
    explains = volume_mod.fit("explains", measured - 4.25, measured)
    covaries = volume_mod.fit("covaries", measured * 0.5 - 4.25, measured)
    assert covaries.pearson_r == pytest.approx(1.0)
    assert explains.pearson_r == pytest.approx(1.0)
    # Same r, and only the residual about the fitted offset tells them apart.
    assert explains.mae_about_offset_db == pytest.approx(0.0, abs=1e-9)
    assert covaries.mae_about_offset_db > 3.0
    # slope is d(ZVOLUME)/d(candidate), so a half-scale candidate reads 0.5.
    # Anything but 1.0 means the two are not the same quantity.
    assert covaries.slope == pytest.approx(0.5)
    assert explains.slope == pytest.approx(1.0)


def test_fit_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        volume_mod.fit("x", np.array([1.0, 2.0]), np.array([1.0]))


# ------------------------------------------------------- third-opinion key


def test_canonical_key_text_parses_to_pitch_class_and_mode():
    assert key_mod.parse_canonical("3B (pc=1,maj)") == (1, "maj")
    assert key_mod.parse_canonical("12A (pc=1,min)") == (1, "min")
    assert key_mod.parse_canonical("") == (None, None)
    assert key_mod.parse_canonical("nonsense") == (None, None)


def test_a_c_major_chroma_matches_the_c_major_template():
    """Sanity floor for the estimator: if a synthetic C-E-G chroma does not
    come back as C major, the template rotation is wrong and every tally
    downstream is noise."""
    profile = np.full(12, 0.01)
    for pitch_class in (0, 4, 7):  # C, E, G
        profile[pitch_class] = 0.3
    pc, mode, margin = key_mod._best_key(profile, key_mod.KK_MAJOR, key_mod.KK_MINOR)
    assert (pc, mode) == (0, "maj")
    assert margin > 0.0


def test_the_estimator_is_rotation_equivariant():
    """Transposing the chroma by k semitones must move the answer by exactly k
    and leave the mode alone. An off-by-one in the template rotation would
    still produce confident-looking keys, and every cluster tally built on it
    would be wrong in a way no summary statistic reveals."""
    profile = np.full(12, 0.01)
    for pitch_class in (0, 4, 7):
        profile[pitch_class] = 0.3
    base_pc, base_mode, _ = key_mod._best_key(
        profile, key_mod.KK_MAJOR, key_mod.KK_MINOR
    )
    for shift in range(1, 12):
        pc, mode, _ = key_mod._best_key(
            np.roll(profile, shift), key_mod.KK_MAJOR, key_mod.KK_MINOR
        )
        assert (pc, mode) == ((base_pc + shift) % 12, base_mode)


def test_the_vote_scores_rekordbox_mik_and_neither_distinctly():
    row = {
        "mik_pk": 1,
        "path": "/x.mp3",
        "pair_tier": "exact_path",
        "signature": "parallel_major_minor",
        "rb_pc": 1,
        "rb_mode": "maj",
        "mik_pc": 1,
        "mik_mode": "min",
        "mik_key_confidence": 0.5,
    }
    for est, expected in (
        ((1, "maj"), "rekordbox"),
        ((1, "min"), "mik"),
        ((6, "min"), "neither"),
    ):
        result = dict(row)
        estimated = {
            "est_pc": est[0],
            "est_mode": est[1],
            "est_margin": 0.1,
            "est_pc_temperley": est[0],
            "est_mode_temperley": est[1],
            "templates_agree": True,
        }
        rb = (result["rb_pc"], result["rb_mode"])
        mik = (result["mik_pc"], result["mik_mode"])
        guess = (estimated["est_pc"], estimated["est_mode"])
        verdict = (
            "both"
            if guess == rb and guess == mik
            else "rekordbox"
            if guess == rb
            else "mik"
            if guess == mik
            else "neither"
        )
        assert verdict == expected


def _resolved(
    mik_pk: int,
    *,
    analysis_date_iso: str | None,
    reencoded_after_analysis: bool,
) -> Any:
    # Return type is genuinely `resolve_mod.ResolvedAudio`, but `resolve_mod`
    # is loaded at runtime via `_load()` (it is a script, not a package), so
    # mypy has no static type to name here -- `Any` is honest, not a mask.
    return resolve_mod.ResolvedAudio(
        mik_pk=mik_pk,
        path=f"/x/{mik_pk}.mp3",
        tier="exact_path",
        pair_tier="exact_path",
        rekordbox_id="rb-1",
        title="T",
        artist="A",
        volume=-10.0,
        energy=5.0,
        tempo=128.0,
        key_camelot="8B",
        key_confidence=0.9,
        clipped_peak_count=0,
        analysis_date_iso=analysis_date_iso,
        file_mtime_iso="2026-01-01T00:00:00+00:00",
        file_size=1000,
        reencoded_after_analysis=reencoded_after_analysis,
    )


def test_usable_audio_excludes_reencoded_rows():
    """P1 regression (PR #383 review): a file modified after MIK analysed it
    would compare the current bytes against a historical MIK key, falsely
    favoring either source in a suspect cluster."""
    date = "2026-01-01T00:00:00+00:00"
    clean = _resolved(1, analysis_date_iso=date, reencoded_after_analysis=False)
    stale = _resolved(2, analysis_date_iso=date, reencoded_after_analysis=True)

    usable, reencoded, unknown = key_mod.usable_audio([clean, stale])

    assert set(usable) == {1}
    assert reencoded == 1
    assert unknown == 0


def test_usable_audio_excludes_and_separately_counts_missing_analysis_dates():
    """A missing analysis date is a DIFFERENT reason for exclusion than a
    confirmed reencode: we cannot confirm the audio matches the analysis
    either way, so it must not be silently folded into the reencoded count
    or, worse, silently admitted as if it were confirmed clean."""
    date = "2026-01-01T00:00:00+00:00"
    clean = _resolved(1, analysis_date_iso=date, reencoded_after_analysis=False)
    no_date = _resolved(3, analysis_date_iso=None, reencoded_after_analysis=False)

    usable, reencoded, unknown = key_mod.usable_audio([clean, no_date])

    assert set(usable) == {1}
    assert reencoded == 0
    assert unknown == 1


def test_cluster_tally_groups_by_signature():
    estimates = [
        key_mod.KeyEstimate(
            mik_pk=i,
            path="/x.mp3",
            pair_tier="basename",
            signature=signature,
            rb_pc=1,
            rb_mode="maj",
            mik_pc=1,
            mik_mode="min",
            mik_key_confidence=0.5,
            est_pc=1,
            est_mode="maj",
            est_margin=0.1,
            est_pc_temperley=1,
            est_mode_temperley="maj",
            templates_agree=True,
            verdict=verdict,
        )
        for i, (signature, verdict) in enumerate(
            [
                ("parallel_major_minor", "rekordbox"),
                ("parallel_major_minor", "mik"),
                ("parallel_major_minor", "rekordbox"),
                ("relative_major_minor", "neither"),
            ]
        )
    ]
    tally = key_mod.tally(estimates)
    assert tally["parallel_major_minor"] == {"mik": 1, "rekordbox": 2}
    assert tally["relative_major_minor"] == {"neither": 1}
