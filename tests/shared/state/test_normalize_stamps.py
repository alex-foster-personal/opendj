"""The one-shot repair for stored timestamps the protocol cannot order.

Contract under test: ``apps/shared/state/normalize_stamps.py``, answering
round 2 finding N3b's second half. ``sync_stamp.coalesce_stored_stamp`` stops
one bad row bricking the machine; it does not make the row right -- a row
stuck at EPOCH loses every conflict it takes part in, silently. This module
is how an operator puts such rows back on the UTC line.

Acceptance criteria, one assertion block each:
- if the scan does not find a naive stored stamp, the row that bricks
  spoke_push is invisible to the repair -- broken.
- if the scan rewrites an orderable but non-canonical spelling, the pass
  churns rows the protocol already handles -- broken.
- if --dry-run writes anything, the flag is a lie -- broken.
- if --live leaves any stored stamp unorderable, the repair did not repair --
  broken.
- if the swept column list drifts from the protocol's digest set, a table can
  hold an unorderable stamp the repair never visits -- broken.
- if neither or both of --dry-run/--live is accepted, the CLI has a hidden
  default about writing to a database -- broken.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import normalize_stamps, sync_stamp
from apps.sync_hub import protocol as hub_protocol

pytestmark = pytest.mark.requirement("INFRA-01")

SID = "c" * 40
CANONICAL = "2026-08-30T09:00:00.000000+00:00"


def _seed_track(conn: sqlite3.Connection, stable_id: str, *, updated_at: str) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, created_at, "
        "updated_at) VALUES (?, 'inferred', 'legacy', ?, ?)",
        (stable_id, CANONICAL, updated_at),
    )


# ----- (1) what the scan finds, and what it leaves alone -------------------


def test_a_naive_stamp_is_repaired_as_that_wall_time_in_utc(
    state_conn: sqlite3.Connection,
) -> None:
    _seed_track(state_conn, SID, updated_at="2024-11-01T12:00:00")
    repairs = normalize_stamps.scan(state_conn)
    assert len(repairs) == 1
    repair = repairs[0]
    assert (repair.table, repair.column) == ("tracks", "updated_at")
    assert repair.reason == "naive-assumed-utc"
    assert repair.replacement == "2024-11-01T12:00:00.000000+00:00"


def test_sqlite_s_own_current_timestamp_spelling_is_repaired(
    state_conn: sqlite3.Connection,
) -> None:
    """``'2024-11-01 12:00:00'`` trips the rejector identically, and it is
    the spelling a hand-written ``CURRENT_TIMESTAMP`` produces."""
    _seed_track(state_conn, SID, updated_at="2024-11-01 12:00:00")
    repairs = normalize_stamps.scan(state_conn)
    assert [r.reason for r in repairs] == ["naive-assumed-utc"]
    assert repairs[0].replacement == "2024-11-01T12:00:00.000000+00:00"


def test_an_unrecoverable_value_becomes_the_lowest_storable_stamp(
    state_conn: sqlite3.Connection,
) -> None:
    _seed_track(state_conn, SID, updated_at="whenever")
    repairs = normalize_stamps.scan(state_conn)
    assert [r.reason for r in repairs] == ["unparseable-to-floor"]
    assert repairs[0].replacement == normalize_stamps.FLOOR_STAMP


def test_the_floor_is_storable_and_the_epoch_sentinel_is_not() -> None:
    """The distinction is load-bearing, so it gets its own assertion.

    ``sync_stamp.EPOCH`` is year zero: it sorts below everything under string
    comparison, which is all a SENTINEL has to do, but ``datetime`` cannot
    represent it. A repair that wrote it into a row would hand
    ``protocol.canonical_timestamp`` a value it refuses on every read -- the
    exact brick this pass exists to clear.
    """
    with pytest.raises(sync_stamp.SyncStampError):
        sync_stamp.parse_canonical(sync_stamp.EPOCH)
    assert (
        sync_stamp.to_canonical(normalize_stamps.FLOOR_STAMP)
        == normalize_stamps.FLOOR_STAMP
    )
    assert sync_stamp.EPOCH < normalize_stamps.FLOOR_STAMP < CANONICAL


@pytest.mark.parametrize(
    "orderable",
    [
        CANONICAL,
        "2026-08-30T09:00:00Z",        # Z suffix
        "2026-08-30T09:00:00+00:00",   # second precision
        "2026-08-30T10:00:00+01:00",   # non-UTC offset
    ],
)
def test_an_orderable_stamp_is_never_rewritten(
    state_conn: sqlite3.Connection, orderable: str,
) -> None:
    """The protocol boundary re-emits these canonically on every read, so
    rewriting them would churn rows for no correctness gain."""
    _seed_track(state_conn, SID, updated_at=orderable)
    assert normalize_stamps.scan(state_conn) == []


def test_deleted_at_is_swept_too(state_conn: sqlite3.Connection) -> None:
    """``deleted_at`` is in the digest and is hashed verbatim (round 2 N8),
    so an unorderable tombstone stamp is a divergence waiting to happen."""
    _seed_track(state_conn, SID, updated_at=CANONICAL)
    state_conn.execute(
        "UPDATE tracks SET deleted_at = '2024-11-01 12:00:00' WHERE stable_id = ?",
        (SID,),
    )
    repairs = normalize_stamps.scan(state_conn)
    assert [(r.table, r.column) for r in repairs] == [("tracks", "deleted_at")]


# ----- (2) applying the repair ---------------------------------------------


def test_applying_the_repair_makes_every_stored_stamp_orderable(
    state_conn: sqlite3.Connection,
) -> None:
    _seed_track(state_conn, SID, updated_at="2024-11-01T12:00:00")
    _seed_track(state_conn, "d" * 40, updated_at="not a timestamp")
    _seed_track(state_conn, "e" * 40, updated_at=CANONICAL)

    outcome = normalize_stamps.apply_repairs(
        state_conn, normalize_stamps.scan(state_conn)
    )
    assert outcome.applied == 2
    assert normalize_stamps.scan(state_conn) == [], "the repair is idempotent"

    for (stored,) in state_conn.execute("SELECT updated_at FROM tracks"):
        # The whole point: sync_hub.protocol.canonical_timestamp no longer
        # raises on any of them, so spoke_push can offer the library again.
        assert sync_stamp.to_canonical(stored) == stored


def test_the_repair_is_one_transaction(state_conn: sqlite3.Connection) -> None:
    """A half-applied repair would leave the operator guessing which rows
    were touched, which is the N3a failure wearing a different hat.

    The failure is injected as a repair naming a column that does not exist,
    so the last UPDATE raises after the earlier ones have run.
    """
    _seed_track(state_conn, SID, updated_at="2024-11-01T12:00:00")
    _seed_track(state_conn, "d" * 40, updated_at="2024-11-02T12:00:00")
    repairs = normalize_stamps.scan(state_conn)
    assert len(repairs) == 2

    doomed = normalize_stamps.Repair(
        table="tracks", column="no_such_column", rowid=1,
        stored="whatever", replacement=CANONICAL,
        reason="unparseable-to-floor",
    )
    with pytest.raises(sqlite3.OperationalError):
        normalize_stamps.apply_repairs(state_conn, [*repairs, doomed])

    assert len(normalize_stamps.scan(state_conn)) == 2, (
        "a failed repair must leave every row exactly as it was"
    )


# ----- (3) the swept list cannot silently shrink ---------------------------


def test_the_swept_columns_cover_the_whole_digest_set() -> None:
    """A digest table missing from the sweep can hold an unorderable stamp
    the repair never visits, which is a machine nothing can unbrick."""
    swept = {table for table, _column in normalize_stamps.STAMP_COLUMNS}
    assert set(hub_protocol.DIGEST_TABLES) <= swept
    for table in hub_protocol.DIGEST_TABLES:
        columns = {
            column
            for swept_table, column in normalize_stamps.STAMP_COLUMNS
            if swept_table == table
        }
        assert {"updated_at", "deleted_at"} <= columns, table
    assert sync_stamp.LOCAL_CHANGELOG_TABLE in swept, (
        "the spoke changelog carries stamps the push fence reads"
    )


# ----- (4) the CLI ---------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A data dir in the canonical ``<data-dir>/state/state.db`` layout."""
    target = normalize_stamps.state_db_path(tmp_path)
    conn = state_db.open_rw(target)
    try:
        _seed_track(conn, SID, updated_at="2024-11-01T12:00:00")
    finally:
        conn.close()
    return tmp_path


def _stored_stamp(data_dir: Path) -> str:
    conn = sqlite3.connect(str(normalize_stamps.state_db_path(data_dir)))
    try:
        return str(conn.execute("SELECT updated_at FROM tracks").fetchone()[0])
    finally:
        conn.close()


def test_dry_run_reports_every_repair_and_writes_nothing(
    data_dir: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert normalize_stamps.main(["--data-dir", str(data_dir), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "tracks.updated_at" in out
    assert "naive-assumed-utc" in out
    assert "[DRY-RUN]" in out
    assert _stored_stamp(data_dir) == "2024-11-01T12:00:00", (
        "--dry-run must not touch the database"
    )


def test_live_rewrites_the_row(
    data_dir: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert normalize_stamps.main(["--data-dir", str(data_dir), "--live"]) == 0
    assert "[OK] rewrote 1 value(s)" in capsys.readouterr().out
    assert _stored_stamp(data_dir) == "2024-11-01T12:00:00.000000+00:00"


def test_a_live_repair_names_the_hub_rotate_as_the_remaining_step(
    data_dir: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """"[OK] rewrote N value(s)" alone is only true on a SPOKE.

    A repair frees rows TRANSITIVELY, and neither thing this pass does
    re-delivers the freed closure from a hub: ``_reset_sync_fences`` rewrites
    ``sync_state``, whose only writer in the repo is the spoke loop in
    ``apps.sync_hub.client``, so on a hub db it matches zero rows; and
    ``_log_repaired_row`` requeues the repaired row alone, while every spoke
    has already pulled past the entries its dependants were skipped in.

    So the operator was being told the job was done at the exact moment their
    fleet was one manual command short of converging, and the next digest
    compare would mismatch permanently. This pass cannot RUN the rotate --
    ``apps.shared`` must not import ``apps.sync_hub`` -- so it names it.
    """
    assert normalize_stamps.main(["--data-dir", str(data_dir), "--live"]) == 0
    out = capsys.readouterr().out
    assert "python -m apps.sync_hub rotate" in out
    assert "HUB" in out


def test_a_dry_run_does_not_tell_anyone_to_rotate_anything(
    data_dir: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The control. A notice printed unconditionally would pass the test
    above while telling an operator who changed NOTHING to go rotate a
    production hub's generation, forcing a fleet-wide full re-offer for no
    reason."""
    assert normalize_stamps.main(["--data-dir", str(data_dir), "--dry-run"]) == 0
    assert "rotate" not in capsys.readouterr().out


def test_the_rotate_command_that_notice_names_actually_exists() -> None:
    """A remediation message is a claim about the CLI, so verify the claim.

    The whole point of this change was that the previous message named a
    command which does nothing for the case it was printed about. Replacing
    it with a command that does not parse at all would be the same defect
    with a fresh coat of paint, and nothing else in the suite would notice --
    the notice is a string, and strings do not typo-check themselves.
    """
    from apps.sync_hub import maintenance

    args = maintenance._parser().parse_args(["rotate", "--data-dir", "."])
    assert args.command == "rotate"


def test_a_clean_db_says_so_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    state_db.open_rw(normalize_stamps.state_db_path(tmp_path)).close()
    assert normalize_stamps.main(["--data-dir", str(tmp_path), "--dry-run"]) == 0
    assert "[OK]" in capsys.readouterr().out


def test_a_missing_db_fails_loudly_rather_than_reporting_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert normalize_stamps.main(
        ["--data-dir", str(tmp_path / "nope"), "--dry-run"]
    ) == 2
    assert "[ERROR]" in capsys.readouterr().err


@pytest.mark.parametrize("flags", [[], ["--dry-run", "--live"]])
def test_the_write_mode_is_never_defaulted(
    data_dir: Path, flags: list[str],
) -> None:
    """Neither flag, or both, must be an argparse error. A pass that writes
    to a database by default is a hidden default with a blast radius."""
    with pytest.raises(SystemExit):
        normalize_stamps.main(["--data-dir", str(data_dir), *flags])


# ----- (5) round 5: a repair must reach the peer, not just the row ---------


def test_a_repaired_row_is_logged_so_the_push_fence_can_offer_it(
    state_conn: sqlite3.Connection,
) -> None:
    """Repairing a row without logging it leaves it out of the sync set.

    A machine that has ALREADY completed one sync offers only what
    ``local_changelog`` recorded above its watermark. Before round 5 this
    pass rewrote the stamp and appended nothing, so a row freed from
    quarantine stayed out of the offer forever and the post-sync digest
    compare failed permanently. Reproduced against the real library:
    ``SyncDigestMismatch`` on ``['playlist_memberships', 'playlists',
    'track_locations', 'track_vendor_ids', 'tracks']`` immediately after a
    successful repair.
    """
    _seed_track(state_conn, SID, updated_at="2024-11-01T12:00:00")
    state_conn.execute("DELETE FROM local_changelog")

    normalize_stamps.apply_repairs(state_conn, normalize_stamps.scan(state_conn))

    logged = state_conn.execute(
        "SELECT table_name, row_pk, updated_at FROM local_changelog"
    ).fetchall()
    assert logged == [
        ("tracks", sync_stamp.encode_row_pk((SID,)), "2024-11-01T12:00:00.000000+00:00")
    ], "the entry must name the repaired row and carry its REPAIRED stamp"


def test_a_clean_repair_run_logs_nothing(state_conn: sqlite3.Connection) -> None:
    """The control: no repair, no changelog entry.

    Without it the assertion above would pass for a pass that logged on every
    invocation, which would re-offer the library on every operator run.
    """
    _seed_track(state_conn, SID, updated_at=CANONICAL)
    state_conn.execute("DELETE FROM local_changelog")
    outcome = normalize_stamps.apply_repairs(
        state_conn, normalize_stamps.scan(state_conn)
    )
    assert outcome.applied == 0
    assert state_conn.execute("SELECT COUNT(*) FROM local_changelog").fetchone()[0] == 0


def test_a_repair_resets_both_fences_against_every_peer(
    state_conn: sqlite3.Connection,
) -> None:
    """The rows a repair FREES without touching still have to be exchanged.

    Both directions, and the pull half is not symmetry for its own sake:

    * PUSH -- quarantine is transitive over the foreign keys
      (:mod:`apps.sync_hub.sync_set`), so repairing one ``tracks`` row also
      frees its ``track_fields``, the playlists whose membership bundles
      named it, and those playlists' pins -- none of which this pass writes
      to, so none of which it can log. ``last_sync_at = NULL`` is what
      ``Watermark.needs_full_offer`` reads.
    * PULL -- while the local row could not be ordered,
      ``apps.sync_hub.engine_apply`` REFUSED the peer's copy of it, and the
      pull watermark advanced over that entry anyway. Nothing replays a
      refusal, so leaving this floor parked made the peer's edit permanently
      unreachable and every later sync raise ``SyncDigestMismatch`` while
      the peer reported success.

    Both are the primitive ``client._watermark_after_hello`` already uses
    after a hub restore, where it resets both floors for the same reason.
    """
    _seed_track(state_conn, SID, updated_at="2024-11-01T12:00:00")
    state_conn.execute(
        "INSERT INTO sync_state(peer, last_push_seq, last_pull_seq, "
        "last_sync_at, peer_generation) VALUES ('peer-1', 9, 9, ?, 'g1')",
        (CANONICAL,),
    )

    normalize_stamps.apply_repairs(state_conn, normalize_stamps.scan(state_conn))

    assert state_conn.execute(
        "SELECT last_push_seq, last_sync_at, last_pull_seq FROM sync_state"
    ).fetchone() == (0, None, 0), (
        "both floors reset, or a row the peer sent while this machine was "
        "quarantined never comes back"
    )


def test_a_run_with_nothing_to_repair_still_resets_both_fences(
    state_conn: sqlite3.Connection,
) -> None:
    """B1c: the fence reset is the half a CLEAN machine runs this pass for.

    Round 5 returned before the reset whenever ``scan`` came back empty --
    which is the state of every healthy peer -- so the recovery the quarantine
    log prints was false on the one machine that needed it. The log names the
    machine that HOLDS the unorderable row; the machine whose pull fence has
    already advanced past the entries the freed rows were skipped in is its
    PEER, and on that peer there is nothing to rewrite.

    The cost is stated rather than dodged: an operator who runs ``--live``
    pays one full re-offer and one full re-pull. LWW rejects on equality, so
    the replay is idempotent (proven three deep in round 1), and the cost of
    NOT paying it was permanent loss. Nothing fires without ``--live``; see
    the dry-run control below.
    """
    _seed_track(state_conn, SID, updated_at=CANONICAL)
    state_conn.execute(
        "INSERT INTO sync_state(peer, last_push_seq, last_pull_seq, "
        "last_sync_at, peer_generation) VALUES ('peer-1', 9, 9, ?, 'g1')",
        (CANONICAL,),
    )
    assert normalize_stamps.scan(state_conn) == [], "nothing to repair here"

    outcome = normalize_stamps.apply_repairs(
        state_conn, normalize_stamps.scan(state_conn)
    )

    assert outcome.applied == 0
    assert outcome.fences_reset == 1, "and it must REPORT the reset it did"
    assert state_conn.execute(
        "SELECT last_push_seq, last_sync_at, last_pull_seq FROM sync_state"
    ).fetchone() == (0, None, 0), (
        "both floors and last_sync_at: needs_full_offer reads the last, and "
        "only a full offer reaches rows that predate local_changelog"
    )


def test_a_dry_run_moves_no_fence(tmp_path: Path) -> None:
    """The control for the test above: nothing happens without ``--live``.

    Without this, "the reset always fires" would be indistinguishable from "the
    reset fires even when the operator only asked to look".
    """
    data_dir = tmp_path / "spoke"
    (data_dir / "state").mkdir(parents=True)
    conn = state_db.open_rw(normalize_stamps.state_db_path(data_dir))
    try:
        _seed_track(conn, SID, updated_at=CANONICAL)
        conn.execute(
            "INSERT INTO sync_state(peer, last_push_seq, last_pull_seq, "
            "last_sync_at, peer_generation) VALUES ('peer-1', 9, 9, ?, 'g1')",
            (CANONICAL,),
        )
        conn.commit()
    finally:
        conn.close()

    assert normalize_stamps.main(
        ["--data-dir", str(data_dir), "--dry-run"]
    ) == 0

    conn = state_db.open_rw(normalize_stamps.state_db_path(data_dir))
    try:
        assert conn.execute(
            "SELECT last_push_seq, last_sync_at, last_pull_seq FROM sync_state"
        ).fetchone() == (9, CANONICAL, 9)
    finally:
        conn.close()


def test_the_logged_pk_map_covers_every_synced_table() -> None:
    """A table missing from the map is repaired and then never offered.

    Pinned against the protocol's own sync set, not a second hand-written
    list: two copies agreeing proves only that they were copied together.
    """
    expected = {spec.name for spec in hub_protocol.SYNC_TABLES}
    expected.add(hub_protocol.MEMBERSHIP_TABLE)
    assert set(normalize_stamps.SYNCED_PKS) == expected
    for spec in hub_protocol.SYNC_TABLES:
        assert normalize_stamps.SYNCED_PKS[spec.name] == spec.pk
    assert (
        normalize_stamps.SYNCED_PKS[hub_protocol.MEMBERSHIP_TABLE]
        == hub_protocol.MEMBERSHIP_SPEC.pk
    )
    # Control: the two changelogs are deliberately ABSENT, because they are
    # machine-local bookkeeping and logging them would be a loop.
    assert sync_stamp.LOCAL_CHANGELOG_TABLE not in normalize_stamps.SYNCED_PKS
    assert "hub_changelog" not in normalize_stamps.SYNCED_PKS


def test_the_hub_changelog_is_swept_too() -> None:
    """A hub that merged a row before this pass existed can hold a bad stamp.

    ``scan`` skips a table that does not exist, so naming it costs a spoke
    nothing.
    """
    swept = {table for table, _column in normalize_stamps.STAMP_COLUMNS}
    assert "hub_changelog" in swept


# ----- (6) round 6: a repair on a hub must be re-pullable, and race-safe ---


def test_a_repaired_row_on_a_hub_gets_a_fresh_hub_changelog_entry(
    state_conn: sqlite3.Connection,
) -> None:
    """Without this a hub-side repair is invisible to every spoke.

    ``hub_changes_since`` only returns entries whose ``seq`` is above a
    spoke's own watermark; the row's ORIGINAL hub_changelog entry (at its
    old, already-pulled seq) never crosses that fence again once repaired,
    so the fix has to be a NEW entry, not just a rewritten row.
    """
    _seed_track(state_conn, SID, updated_at="2024-11-01T12:00:00")
    state_conn.execute("DELETE FROM hub_changelog")

    normalize_stamps.apply_repairs(state_conn, normalize_stamps.scan(state_conn))

    logged = state_conn.execute(
        "SELECT table_name, row_pk, updated_at FROM hub_changelog"
    ).fetchall()
    assert logged == [
        ("tracks", sync_stamp.encode_row_pk((SID,)), "2024-11-01T12:00:00.000000+00:00")
    ], "the entry must name the repaired row and carry its REPAIRED stamp"


def test_a_clean_repair_run_logs_nothing_to_hub_changelog_either(
    state_conn: sqlite3.Connection,
) -> None:
    """The control: no repair, no hub_changelog entry either."""
    _seed_track(state_conn, SID, updated_at=CANONICAL)
    state_conn.execute("DELETE FROM hub_changelog")
    outcome = normalize_stamps.apply_repairs(
        state_conn, normalize_stamps.scan(state_conn)
    )
    assert outcome.applied == 0
    assert state_conn.execute("SELECT COUNT(*) FROM hub_changelog").fetchone()[0] == 0


def test_a_concurrent_write_in_the_scan_to_lock_gap_is_not_clobbered(
    state_conn: sqlite3.Connection,
) -> None:
    """``scan()`` reads before the write transaction's ``BEGIN IMMEDIATE``.

    A writer that lands a newer, already-orderable value in that gap must
    win: an unconditional ``UPDATE ... WHERE rowid = ?`` would overwrite it
    with the stale repair replacement and silently change LWW ordering for
    the row (round 6 finding).
    """
    _seed_track(state_conn, SID, updated_at="2024-11-01T12:00:00")
    repairs = normalize_stamps.scan(state_conn)
    assert len(repairs) == 1

    newer = "2026-01-01T00:00:00.000000+00:00"
    state_conn.execute(
        "UPDATE tracks SET updated_at = ? WHERE stable_id = ?", (newer, SID)
    )

    outcome = normalize_stamps.apply_repairs(state_conn, repairs)

    assert outcome.applied == 0, "the stale repair must not have been counted as applied"
    assert (
        state_conn.execute("SELECT updated_at FROM tracks").fetchone()[0] == newer
    ), "the concurrent writer's newer value must survive"


def test_a_repair_still_applies_and_logs_once_when_nothing_races_it(
    state_conn: sqlite3.Connection,
) -> None:
    """The control: the ordinary path is unaffected by the race guard."""
    _seed_track(state_conn, SID, updated_at="2024-11-01T12:00:00")
    repairs = normalize_stamps.scan(state_conn)

    outcome = normalize_stamps.apply_repairs(state_conn, repairs)

    assert outcome.applied == 1
    assert (
        state_conn.execute("SELECT updated_at FROM tracks").fetchone()[0]
        == "2024-11-01T12:00:00.000000+00:00"
    )
