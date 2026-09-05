"""Precedence-at-promotion and duplicate-target tests split out of
:mod:`tests.mik.test_promote` (the file-size review gate). The ``staged``
fixture and the ``ALL_PASSED``/``STAGED_FIELD_COUNT``/``_stage_one`` helpers
live in ``conftest.py`` so both modules share one definition rather than
each redefining them.

[if] a row is promoted to known/unknown/deleted/claimed [then] it writes once/raises, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.mik import load as loader
from apps.mik import match as matcher
from apps.mik import mikdb
from apps.mik import promote as promoter
from apps.shared.equivalence import EquivalenceGate
from apps.shared.state import provenance as _prov

from .conftest import ALL_PASSED, STAGED_FIELD_COUNT, _stage_one

pytestmark = pytest.mark.requirement("META-01")

# ------------------------------------------------- precedence at promotion

# ------------------------------------------------- precedence at promotion


def test_precedence_blocks_a_field_already_supplied_by_another_source(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    """A track that acquired ``energy`` from rekordbox while MIK's analysis sat
    in staging keeps rekordbox's value: promotion is not a backdoor around
    ``apps.mik.load.build_plan``'s precedence policy."""
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    _prov.write_field(
        state_conn,
        stable_id="a" * 40,
        field_name="energy",
        value=5.0,
        source="rekordbox",
        modified_at="2026-01-01T00:00:00+00:00",
        confidence=0.9,
        actor="test",
    )
    result = promoter.promote(
        state_conn,
        staged["gate"],
        source_row_id=staged["source_row_id"],
        stable_id="a" * 40,
    )
    assert "energy" in result.blocked_by_precedence
    assert (
        state_conn.execute(
            "SELECT source FROM track_fields WHERE field_name='energy'"
        ).fetchone()[0]
        == "rekordbox"
    )
    # Left staged, not consumed: a later attempt can still promote it.
    assert (
        state_conn.execute(
            "SELECT promoted_stable_id FROM unmatched_source_analysis "
            "WHERE field_name='energy'"
        ).fetchone()[0]
        is None
    )


def test_overwrite_flag_does_not_help_a_field_where_the_source_always_outranks_mik(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    """rekordbox always outranks MIK on bpm; ``overwrite_lower_precedence``
    only unblocks a source that is NOT in ``OUTRANKS_MIK`` for the field."""
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    _prov.write_field(
        state_conn,
        stable_id="a" * 40,
        field_name="bpm",
        value=128.0,
        source="rekordbox",
        modified_at="2026-01-01T00:00:00+00:00",
        confidence=1.0,
        actor="test",
    )
    result = promoter.promote(
        state_conn,
        staged["gate"],
        source_row_id=staged["source_row_id"],
        stable_id="a" * 40,
        overwrite_lower_precedence=True,
    )
    assert "bpm" in result.blocked_by_precedence
    assert (
        state_conn.execute(
            "SELECT source FROM track_fields WHERE field_name='bpm'"
        ).fetchone()[0]
        == "rekordbox"
    )


def test_overwrite_flag_lets_mik_replace_a_non_outranking_source(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    _prov.write_field(
        state_conn,
        stable_id="a" * 40,
        field_name="energy",
        value=1.0,
        source="rekordbox",
        modified_at="2026-01-01T00:00:00+00:00",
        confidence=0.5,
        actor="test",
    )
    result = promoter.promote(
        state_conn,
        staged["gate"],
        source_row_id=staged["source_row_id"],
        stable_id="a" * 40,
        overwrite_lower_precedence=True,
    )
    assert "energy" not in result.blocked_by_precedence
    assert (
        state_conn.execute(
            "SELECT source FROM track_fields WHERE field_name='energy'"
        ).fetchone()[0]
        == "mik"
    )


def test_key_below_confidence_floor_is_blocked_regardless_of_overwrite(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    gate = _stage_one(
        state_conn, make_mik_store, data_dir, write_verdicts, confidence=0.5
    )
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    result = promoter.promote(
        state_conn,
        gate,
        source_row_id="1",
        stable_id="a" * 40,
        overwrite_lower_precedence=True,
    )
    assert result.blocked_by_key_floor == 1
    assert (
        state_conn.execute(
            "SELECT COUNT(*) FROM track_fields WHERE field_name='key'"
        ).fetchone()[0]
        == 0
    )


def test_key_in_review_band_with_a_prior_source_is_flagged_not_written(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    gate = _stage_one(
        state_conn, make_mik_store, data_dir, write_verdicts, confidence=0.8
    )
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    _prov.write_field(
        state_conn,
        stable_id="a" * 40,
        field_name="key",
        value="8B",
        source="rekordbox",
        modified_at="2026-01-01T00:00:00+00:00",
        confidence=0.9,
        actor="test",
    )
    result = promoter.promote(
        state_conn,
        gate,
        source_row_id="1",
        stable_id="a" * 40,
        overwrite_lower_precedence=True,
    )
    assert "a" * 40 in result.key_review_band
    assert (
        state_conn.execute(
            "SELECT value_json FROM track_fields WHERE field_name='key'"
        ).fetchone()[0]
        == '"8B"'
    )


def test_key_in_review_band_with_no_prior_source_still_writes(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    gate = _stage_one(
        state_conn, make_mik_store, data_dir, write_verdicts, confidence=0.8
    )
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    result = promoter.promote(state_conn, gate, source_row_id="1", stable_id="a" * 40)
    assert result.key_review_band == []
    assert (
        state_conn.execute(
            "SELECT COUNT(*) FROM track_fields WHERE field_name='key'"
        ).fetchone()[0]
        == 1
    )


def test_promotion_persists_verification_provenance(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    result = promoter.promote(
        state_conn,
        staged["gate"],
        source_row_id=staged["source_row_id"],
        stable_id="a" * 40,
    )
    assert result.verification_rows == STAGED_FIELD_COUNT
    rows = state_conn.execute(
        "SELECT field_name, status FROM analysis_field_verification "
        "WHERE source='mik'"
    ).fetchall()
    assert {row[0] for row in rows} == set(loader.GATED_FIELDS)
    assert all(status == "passed" for _, status in rows)


def test_promote_all_is_atomic(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    with pytest.raises(promoter.PromotionError):
        promoter.promote_all(
            state_conn,
            staged["gate"],
            {"1": "a" * 40, "404": "a" * 40},
        )
    assert state_conn.execute("SELECT COUNT(*) FROM track_fields").fetchone()[0] == 0


# ------------------------------------------------- duplicate promotion targets


def test_discover_rejects_duplicate_promotion_targets(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    """P1 regression (PR #383 review, apps/mik/promote.py:177): two staged
    MIK rows sharing a path must not both be assigned the SAME stable_id once
    that path resolves to one newly created track. ``promote_all`` would then
    write both -- and since both are MIK, the second is not precedence-
    blocked and silently overwrites the first's scalar and segment analysis,
    with the final value depending on sort order rather than any real
    ranking. ``discover`` must instead rank claimants the same way
    ``match_songs`` resolves the identical collision (highest confidence
    wins here, since both claim via the same exact_path tier) and leave the
    loser staged."""
    write_verdicts(ALL_PASSED)
    store = make_mik_store(
        [
            {
                "pk": 1,
                "path": "/Users/dev/dup.mp3",
                "name": "7 - Dup",
                "artist": "Nobody",
                "key": "9A",
                "energy": 7.0,
                "confidence": 0.40,
                "segments": [(0.0, 60.0, 5)],
            },
            {
                "pk": 2,
                "path": "/Users/dev/dup.mp3",
                "name": "7 - Dup",
                "artist": "Nobody",
                "key": "9A",
                "energy": 9.0,
                "confidence": 0.95,
                "segments": [(0.0, 60.0, 8)],
            },
        ]
    )
    songs, _ = mikdb.read_songs(mikdb.open_ro(store))
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs(songs, index)
    gate = EquivalenceGate.load(data_dir)
    plan = loader.build_plan(songs, report, gate, state_conn)
    loader.apply_plan(state_conn, plan)

    # Neither song has a candidate yet: both land in staging, no collision.
    assert promoter.discover(state_conn) == {}

    # The shared path now resolves to exactly one newly created track.
    add_track("a" * 40, file_path="/Users/dev/dup.mp3")

    found = promoter.discover(state_conn)
    # Exactly ONE source row claims the track -- never both.
    assert list(found.values()).count("a" * 40) == 1
    # The higher-confidence song (pk 2, source_row_id "2") wins the rank;
    # pk 1 is left staged rather than silently losing its analysis later.
    assert found == {"2": "a" * 40}

    results = promoter.promote_all(state_conn, gate, found)
    assert len(results) == 1
    energy = state_conn.execute(
        "SELECT value_json FROM track_fields WHERE stable_id = ? AND field_name = 'energy'",
        ("a" * 40,),
    ).fetchone()[0]
    assert energy == "9"
    assert promoter.pending_counts(state_conn) == {"no_candidate": 1}
