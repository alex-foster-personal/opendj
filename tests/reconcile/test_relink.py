"""Tests for the relink writer, the undo path and the ambiguous tie-breakers.

Ties to RECON-01/02. Everything here runs against a throwaway ``state.db`` under
``tmp_path`` built by :func:`_make_state_db`; no test can reach the real library.

The load-bearing guarantees, each pinned by a named test below:

* dry-run is the default and leaves the DB byte-identical,
* the reversal log exists before the first UPDATE,
* a relink changes ``file_path`` and nothing else,
* one bad row rolls the whole batch back,
* undo restores every row and is idempotent,
* a tie is never broken by folder plausibility alone.

Failure lines read as statements about behaviour: "if X then broken".
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from apps.reconcile import __main__ as reconcile_cli
from apps.reconcile import index_disk, match, relink, tiebreak
from apps.shared.state import db as state_db_mod
from apps.shared.state.writer import StateWriter
from tests.reconcile.conftest import HAS_REAL_LIBRARY

# ----- fixtures ---------------------------------------------------------


def _make_state_db(path: Path, rows: list[dict[str, object]]) -> Path:
    """Create a state DB holding ``rows`` via the real writer path."""
    conn = state_db_mod.open_rw(path)
    try:
        with StateWriter(conn, actor="test") as writer:
            for row in rows:
                writer.upsert_track(
                    stable_id=str(row["stable_id"]),
                    stable_id_tier=str(row.get("tier", "inferred")),
                    title=row.get("title"),  # type: ignore[arg-type]
                    artists=list(row.get("artists", [])),  # type: ignore[arg-type]
                    album=row.get("album"),  # type: ignore[arg-type]
                    isrc=row.get("isrc"),  # type: ignore[arg-type]
                    duration_ms=row.get("duration_ms"),  # type: ignore[arg-type]
                    file_path=row.get("file_path"),  # type: ignore[arg-type]
                    content_hash=row.get("content_hash"),  # type: ignore[arg-type]
                )
    finally:
        conn.close()
    return path


def _rows(db: Path) -> dict[str, tuple]:
    """Every ``tracks`` row keyed by stable_id, for before/after comparison."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            f"SELECT {', '.join(relink.TRACK_COLUMNS)} FROM tracks ORDER BY stable_id"
        )
        return {r[0]: tuple(r) for r in cur.fetchall()}
    finally:
        conn.close()


def _entry(sid: str, old: str, new: str, conf: float = 0.95) -> relink.RelinkEntry:
    return relink.RelinkEntry(
        stable_id=sid,
        old_path=old,
        new_path=new,
        tier="basename-exact-unique",
        confidence=conf,
        timestamp="2026-07-28T00:00:00+00:00",
    )


def _audio(root: Path, name: str, payload: bytes = b"x" * 64) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    dst = root / name
    dst.write_bytes(payload)
    return dst


@pytest.fixture
def ambient_temp_neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real files under a classification-neutral ancestry; production untouched.

    ``tiebreak.classify_path`` substring-matches the candidate string as
    given, and "/tmp/" is a real STAGING_SEGMENTS entry. pytest's tmp_path is
    /tmp/pytest-of-*/... on Linux but /private/var/folders/... on macOS, so a
    "Manual Library" fixture spelled as an ABSOLUTE path classified curated
    on macOS and staging on Linux. That is correct production behaviour (a
    library sitting in the system temp dir IS a staging copy), but it means
    the ambient temp root, not the folder names the test spells out, decides
    the class.

    Neutral ancestry without altering any production global: chdir into
    tmp_path and hand the scorer candidate paths RELATIVE to it
    ("Manual Library/dup.mp3"). ``os.path.exists`` resolves them against the
    cwd, so the files are REAL on disk (``score_rows`` hard-rejects a missing
    candidate at apps/reconcile/tiebreak.py:296, which short-circuits
    ``_verdict`` before the RESOLVE_MARGIN rule these tests pin -- synthetic
    paths would make them pass vacuously), while the string ``classify_path``
    sees contains only the folder names the test chose. STAGING_SEGMENTS and
    CURATED_SEGMENTS ship exactly as production defines them; the "/tmp/"
    rule stays covered by the classify_path table below.
    """
    monkeypatch.chdir(tmp_path)


def _disk_entry(
    path: str,
    *,
    size: int = 1000,
    duration: float | None = None,
    isrc: str | None = None,
    title: str | None = None,
    artist: str | None = None,
    dataless: bool = False,
) -> index_disk.DiskAudio:
    return index_disk.DiskAudio(
        path=path,
        size_bytes=size,
        mtime=0.0,
        tags_read=True,
        duration_s=duration,
        title=title,
        artist=artist,
        isrc=isrc,
        dataless=dataless,
    )


def _result(
    sid: str,
    *,
    bucket: str = "relinkable-auto",
    path: str | None = "/gone/x.mp3",
    candidates: list[match.Candidate] | None = None,
    ambiguity: str | None = None,
) -> match.RowResult:
    return match.RowResult(
        stable_id=sid,
        file_path=path,
        bucket=bucket,  # type: ignore[arg-type]
        reason="test",
        tier=candidates[0].tier if candidates else None,  # type: ignore[index]
        ambiguity=ambiguity,  # type: ignore[arg-type]
        candidates=candidates or [],
    )


def _cand(path: str, conf: float = 0.95, tier: str = "basename-exact-unique"):
    return match.Candidate(path=path, tier=tier, confidence=conf, reason="test")  # type: ignore[arg-type]


# ----- selection --------------------------------------------------------


@pytest.mark.requirement("RECON-02")
def test_selection_keeps_only_requested_bucket_above_confidence() -> None:
    results = [
        _result("auto-strong", candidates=[_cand("/disk/a.mp3", 0.95)]),
        _result("auto-weak", candidates=[_cand("/disk/b.mp3", 0.70)]),
        _result(
            "amb",
            bucket="relinkable-ambiguous",
            candidates=[_cand("/disk/c.mp3", 0.95)],
            ambiguity="multiple-above-threshold",
        ),
    ]
    picked = relink.select_entries(
        results, buckets=["relinkable-auto"], min_confidence=0.85
    )
    assert [e.stable_id for e in picked] == ["auto-strong"], (
        "if a below-threshold or out-of-bucket row is selected then the "
        "confidence floor and the bucket filter are not being enforced"
    )
    assert picked[0].new_path == "/disk/a.mp3"


@pytest.mark.requirement("RECON-01")
@pytest.mark.parametrize(
    "bucket", ["present", "awaiting-volume", "streaming", "absent-no-audio"]
)
def test_selection_refuses_non_relinkable_buckets(bucket: str) -> None:
    with pytest.raises(ValueError, match="never relinkable"):
        relink.select_entries([], buckets=[bucket], min_confidence=0.0)


@pytest.mark.requirement("RECON-01")
def test_selection_refuses_unknown_bucket_rather_than_ignoring_it() -> None:
    with pytest.raises(ValueError, match="unknown bucket"):
        relink.select_entries([], buckets=["relinkable-maybe"], min_confidence=0.0)


@pytest.mark.requirement("RECON-02")
def test_selection_limit_takes_the_strongest_evidence_first() -> None:
    results = [
        _result("weaker", path="/gone/1.mp3", candidates=[_cand("/disk/1.mp3", 0.88)]),
        _result("stronger", path="/gone/2.mp3", candidates=[_cand("/disk/2.mp3", 0.95)]),
    ]
    picked = relink.select_entries(
        results, buckets=["relinkable-auto"], min_confidence=0.85, limit=1
    )
    assert [e.stable_id for e in picked] == ["stronger"], (
        "if --limit takes an arbitrary row then two runs with the same limit "
        "apply different repairs"
    )


@pytest.mark.requirement("RECON-01")
def test_selection_skips_rows_with_no_recorded_path() -> None:
    results = [_result("blank", path=None, candidates=[_cand("/disk/a.mp3")])]
    assert relink.select_entries(
        results, buckets=["relinkable-auto"], min_confidence=0.0
    ) == [], "if a NULL old_path is selected then the change cannot be reversed"


# ----- pre-write safety re-checks ---------------------------------------


@pytest.mark.requirement("RECON-01")
def test_reject_target_that_vanished_since_classification(tmp_path: Path) -> None:
    gone = tmp_path / "poof.mp3"
    ok, rejected = relink.reject_unsafe(
        [_entry("a", "/gone/old.mp3", str(gone))], all_recorded_paths=[]
    )
    assert ok == []
    assert "no longer exists" in rejected[0].reason, (
        "if a vanished candidate is applied then state.db gains a fresh dead link"
    )


@pytest.mark.requirement("RECON-01")
def test_reject_target_already_recorded_by_another_row(tmp_path: Path) -> None:
    target = _audio(tmp_path, "shared.mp3")
    ok, rejected = relink.reject_unsafe(
        [_entry("a", "/gone/old.mp3", str(target))],
        all_recorded_paths=["/gone/old.mp3", str(target)],
    )
    assert ok == []
    assert "already records this exact path" in rejected[0].reason, (
        "if a path recorded by another row is applied then two rows share one file"
    )


@pytest.mark.requirement("RECON-01")
def test_reject_two_entries_claiming_one_file(tmp_path: Path) -> None:
    target = _audio(tmp_path, "one.mp3")
    ok, rejected = relink.reject_unsafe(
        [
            _entry("a", "/gone/a.mp3", str(target)),
            _entry("b", "/gone/b.mp3", str(target)),
        ],
        all_recorded_paths=["/gone/a.mp3", "/gone/b.mp3"],
    )
    assert [e.stable_id for e in ok] == ["a"]
    assert "already claimed in this batch" in rejected[0].reason


@pytest.mark.requirement("RECON-01")
def test_reject_noop_write_where_new_equals_old(tmp_path: Path) -> None:
    target = _audio(tmp_path, "same.mp3")
    ok, rejected = relink.reject_unsafe(
        [_entry("a", str(target), str(target))], all_recorded_paths=[str(target)]
    )
    assert ok == [] and "equals old path" in rejected[0].reason


# ----- reversal log -----------------------------------------------------


@pytest.mark.requirement("RECON-01")
def test_log_columns_are_exactly_the_specified_six(tmp_path: Path) -> None:
    out = relink.write_log([_entry("a", "/old.mp3", "/new.mp3")], tmp_path / "log.csv")
    with out.open(encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert header == list(relink.LOG_COLUMNS) == [
        "stable_id",
        "old_path",
        "new_path",
        "tier",
        "confidence",
        "timestamp",
    ]


@pytest.mark.requirement("RECON-01")
def test_log_round_trips_through_read_log(tmp_path: Path) -> None:
    entries = [_entry("a", "/old-a.mp3", "/new-a.mp3", 0.9), _entry("b", "/o.mp3", "/n.mp3")]
    out = relink.write_log(entries, tmp_path / "relink-log-x.csv")
    back = relink.read_log(out)
    assert [(e.stable_id, e.old_path, e.new_path) for e in back] == [
        (e.stable_id, e.old_path, e.new_path) for e in entries
    ]


@pytest.mark.requirement("RECON-01")
def test_read_log_refuses_a_dry_run_log(tmp_path: Path) -> None:
    out = relink.write_log(
        [_entry("a", "/old.mp3", "/new.mp3")],
        relink.log_path_for(tmp_path, live=False),
    )
    assert out.name.endswith(relink.DRY_RUN_LOG_SUFFIX)
    with pytest.raises(ValueError, match="DRY-RUN log"):
        relink.read_log(out)


@pytest.mark.requirement("RECON-01")
def test_read_log_raises_on_a_missing_column(tmp_path: Path) -> None:
    bad = tmp_path / "relink-log-bad.csv"
    bad.write_text("stable_id,old_path\na,/x.mp3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing column"):
        relink.read_log(bad)


@pytest.mark.requirement("RECON-01")
def test_read_log_raises_when_absent(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        relink.read_log(tmp_path / "nope.csv")


# ----- the write --------------------------------------------------------


@pytest.mark.requirement("RECON-01")
def test_apply_changes_file_path_and_nothing_else(tmp_path: Path) -> None:
    target = _audio(tmp_path / "disk", "moved.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [
            {
                "stable_id": "a",
                "title": "Quiet Rooms",
                "artists": ["Orla Finch", "JUNO-K"],
                "album": "Palaces",
                "isrc": "GBAYE2101234",
                "duration_ms": 232000,
                "file_path": "/gone/moved.mp3",
                "content_hash": "deadbeef",
            }
        ],
    )
    before = _rows(db)["a"]
    outcomes = relink.apply_entries(db, [_entry("a", "/gone/moved.mp3", str(target))])
    after = _rows(db)["a"]
    assert [o.outcome for o in outcomes] == ["changed"]
    idx = relink.TRACK_COLUMNS.index("file_path")
    assert after[idx] == str(target)
    for pos, col in enumerate(relink.TRACK_COLUMNS):
        if col in relink.MUTABLE_COLUMNS:
            continue
        assert after[pos] == before[pos], (
            f"if a relink changes {col} then it is not a link repair, it is an edit"
        )


@pytest.mark.requirement("RECON-01")
def test_apply_appends_a_track_update_event(tmp_path: Path) -> None:
    target = _audio(tmp_path / "disk", "ev.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [{"stable_id": "a", "file_path": "/gone/ev.mp3"}],
    )
    relink.apply_entries(db, [_entry("a", "/gone/ev.mp3", str(target))])
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        kinds = [
            r[0]
            for r in conn.execute(
                "SELECT kind FROM events WHERE stable_id = 'a' ORDER BY rowid"
            )
        ]
        actors = {
            r[0] for r in conn.execute("SELECT actor FROM events WHERE kind='track.update'")
        }
    finally:
        conn.close()
    assert kinds[-1] == "track.update", (
        "if a relink appends no event then the durable log cannot explain the change"
    )
    assert actors == {"reconcile.relink"}


@pytest.mark.requirement("RECON-01")
def test_apply_is_idempotent_on_a_second_run(tmp_path: Path) -> None:
    target = _audio(tmp_path / "disk", "idem.mp3")
    db = _make_state_db(
        tmp_path / "state.db", [{"stable_id": "a", "file_path": "/gone/idem.mp3"}]
    )
    entries = [_entry("a", "/gone/idem.mp3", str(target))]
    assert [o.outcome for o in relink.apply_entries(db, entries)] == ["changed"]
    snapshot = _rows(db)
    assert [o.outcome for o in relink.apply_entries(db, entries)] == ["already"]
    assert _rows(db) == snapshot, (
        "if a repeat apply rewrites the row then updated_at churns for no change"
    )


@pytest.mark.requirement("RECON-01")
def test_one_conflicting_row_rolls_the_whole_batch_back(tmp_path: Path) -> None:
    good = _audio(tmp_path / "disk", "good.mp3")
    bad = _audio(tmp_path / "disk", "bad.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [
            {"stable_id": "a", "file_path": "/gone/good.mp3"},
            {"stable_id": "b", "file_path": "/somewhere/else.mp3"},
        ],
    )
    before = _rows(db)
    entries = [
        _entry("a", "/gone/good.mp3", str(good)),
        _entry("b", "/gone/bad.mp3", str(bad)),  # old_path does not match the DB
    ]
    with pytest.raises(RuntimeError, match="drifted"):
        relink.apply_entries(db, entries)
    assert _rows(db) == before, (
        "if a failed batch leaves row a applied then there is no single point "
        "to roll back to and the log describes a state that never existed"
    )


@pytest.mark.requirement("RECON-01")
def test_missing_stable_id_is_a_conflict_not_an_insert(tmp_path: Path) -> None:
    target = _audio(tmp_path / "disk", "ghost.mp3")
    db = _make_state_db(tmp_path / "state.db", [{"stable_id": "a", "file_path": "/x.mp3"}])
    before = _rows(db)
    with pytest.raises(RuntimeError, match="drifted"):
        relink.apply_entries(db, [_entry("ghost", "/gone/ghost.mp3", str(target))])
    assert _rows(db) == before, (
        "if an unknown stable_id inserts a row then relink can invent tracks"
    )


@pytest.mark.requirement("RECON-01")
def test_drift_guard_rolls_back_when_another_column_moves(
    tmp_path: Path, monkeypatch
) -> None:
    """The guard, not the writer, is what makes 'only file_path' true."""
    target = _audio(tmp_path / "disk", "guard.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [{"stable_id": "a", "title": "Keep Me", "file_path": "/gone/guard.mp3"}],
    )
    before = _rows(db)
    real_upsert = StateWriter.upsert_track

    def sneaky(self, **kw):
        kw["title"] = "Clobbered"
        return real_upsert(self, **kw)

    monkeypatch.setattr(StateWriter, "upsert_track", sneaky)
    with pytest.raises(RuntimeError, match="beyond file_path"):
        relink.apply_entries(db, [_entry("a", "/gone/guard.mp3", str(target))])
    assert _rows(db) == before


# ----- undo -------------------------------------------------------------


@pytest.mark.requirement("RECON-01")
def test_undo_restores_every_row_byte_for_byte_except_updated_at(
    tmp_path: Path,
) -> None:
    target = _audio(tmp_path / "disk", "back.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [
            {
                "stable_id": "a",
                "title": "Round Trip",
                "artists": ["A", "B"],
                "album": "LP",
                "isrc": "GBAYE2101234",
                "duration_ms": 1000,
                "file_path": "/gone/back.mp3",
                "content_hash": "cafe",
            }
        ],
    )
    before = _rows(db)["a"]
    entries = [_entry("a", "/gone/back.mp3", str(target))]
    relink.apply_entries(db, entries)
    relink.apply_entries(db, entries, reverse=True, tolerate_conflicts=True)
    after = _rows(db)["a"]
    for pos, col in enumerate(relink.TRACK_COLUMNS):
        if col == "updated_at":
            continue
        assert after[pos] == before[pos], (
            f"if undo does not restore {col} then the repair is not reversible"
        )


@pytest.mark.requirement("RECON-01")
def test_undo_twice_reports_already_and_changes_nothing(tmp_path: Path) -> None:
    target = _audio(tmp_path / "disk", "twice.mp3")
    db = _make_state_db(
        tmp_path / "state.db", [{"stable_id": "a", "file_path": "/gone/twice.mp3"}]
    )
    entries = [_entry("a", "/gone/twice.mp3", str(target))]
    relink.apply_entries(db, entries)
    relink.apply_entries(db, entries, reverse=True, tolerate_conflicts=True)
    snapshot = _rows(db)
    outcomes = relink.apply_entries(db, entries, reverse=True, tolerate_conflicts=True)
    assert [o.outcome for o in outcomes] == ["already"]
    assert _rows(db) == snapshot


@pytest.mark.requirement("RECON-01")
def test_undo_leaves_a_drifted_row_alone_and_reverts_the_rest(tmp_path: Path) -> None:
    one = _audio(tmp_path / "disk", "one.mp3")
    two = _audio(tmp_path / "disk", "two.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [
            {"stable_id": "a", "file_path": "/gone/one.mp3"},
            {"stable_id": "b", "file_path": "/gone/two.mp3"},
        ],
    )
    entries = [
        _entry("a", "/gone/one.mp3", str(one)),
        _entry("b", "/gone/two.mp3", str(two)),
    ]
    relink.apply_entries(db, entries)
    # Someone else edits row b out from under us.
    conn = state_db_mod.open_rw(db, apply_schema=False)
    try:
        conn.execute("UPDATE tracks SET file_path = '/third/party.mp3' WHERE stable_id='b'")
    finally:
        conn.close()
    outcomes = relink.apply_entries(
        db, entries, reverse=True, tolerate_conflicts=True
    )
    by_id = {o.stable_id: o for o in outcomes}
    assert by_id["a"].outcome == "changed"
    assert by_id["b"].outcome == "conflict", (
        "if undo overwrites a third-party edit then it destroys work it did not do"
    )
    rows = _rows(db)
    idx = relink.TRACK_COLUMNS.index("file_path")
    assert rows["a"][idx] == "/gone/one.mp3"
    assert rows["b"][idx] == "/third/party.mp3"


# ----- availability -----------------------------------------------------


@pytest.mark.requirement("RECON-01")
def test_availability_counts_present_and_offline_volumes(tmp_path: Path) -> None:
    here = _audio(tmp_path / "disk", "here.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [
            {"stable_id": "a", "file_path": str(here)},
            {"stable_id": "b", "file_path": "/gone/x.mp3"},
            {"stable_id": "c", "file_path": "/Volumes/NOPE-NOT-MOUNTED/x.mp3"},
        ],
    )
    avail = relink.measure_availability(db)
    assert (avail.total, avail.present, avail.missing) == (3, 1, 2)
    assert avail.awaiting_volume == 1, (
        "if an unmounted-volume row is not counted separately then the "
        "before/after delta looks like data loss"
    )


# ----- CLI --------------------------------------------------------------


def _cli_data_dir(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A tmp data dir plus a disk root holding one relocated file."""
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    disk = tmp_path / "roots" / "Music"
    target = _audio(disk, "relocated.mp3")
    _make_state_db(
        data_dir / "state" / "state.db",
        [{"stable_id": "a", "file_path": "/Users/old/Music/relocated.mp3"}],
    )
    return data_dir, disk, target


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.requirement("RECON-01")
def test_cli_dry_run_is_the_default_and_writes_nothing(tmp_path: Path) -> None:
    data_dir, disk, _ = _cli_data_dir(tmp_path)
    db = data_dir / "state" / "state.db"
    digest = _sha256(db)
    code = relink.cmd_relink(
        ["--data-dir", str(data_dir), "--roots", str(disk),
         "--index-cache", str(tmp_path / "c.json")]
    )
    assert code == 0
    assert _sha256(db) == digest, (
        "if a bare `relink` mutates state.db then dry-run is not the default"
    )
    logs = list((data_dir / "reconcile").glob("relink-log-*"))
    assert len(logs) == 1 and logs[0].name.endswith(relink.DRY_RUN_LOG_SUFFIX)


@pytest.mark.requirement("RECON-01")
def test_cli_live_without_the_risk_flag_refuses(tmp_path: Path) -> None:
    data_dir, disk, _ = _cli_data_dir(tmp_path)
    db = data_dir / "state" / "state.db"
    digest = _sha256(db)
    code = relink.cmd_relink(
        ["--data-dir", str(data_dir), "--roots", str(disk), "--live"]
    )
    assert code == 2
    assert _sha256(db) == digest
    assert not (data_dir / "reconcile").exists(), (
        "if a refused live run still writes a log then the gate runs too late"
    )


@pytest.mark.requirement("RECON-01")
def test_cli_ambiguous_bucket_needs_the_risk_flag_even_dry(tmp_path: Path) -> None:
    data_dir, disk, _ = _cli_data_dir(tmp_path)
    code = relink.cmd_relink(
        [
            "--data-dir",
            str(data_dir),
            "--roots",
            str(disk),
            "--bucket",
            "relinkable-ambiguous",
        ]
    )
    assert code == 2, (
        "if the ambiguous bucket is selectable without the risk flag then the "
        "human-adjudication rule is only a comment"
    )


@pytest.mark.requirement("RECON-01")
def test_cli_live_then_undo_round_trips(tmp_path: Path) -> None:
    data_dir, disk, target = _cli_data_dir(tmp_path)
    db = data_dir / "state" / "state.db"
    before = _rows(db)
    common = [
        "--data-dir",
        str(data_dir),
        "--roots",
        str(disk),
        "--index-cache",
        str(tmp_path / "c.json"),
    ]
    assert relink.cmd_relink([*common, "--live", "--i-understand-the-risks"]) == 0
    idx = relink.TRACK_COLUMNS.index("file_path")
    assert _rows(db)["a"][idx] == str(target)

    logs = [
        p
        for p in (data_dir / "reconcile").glob("relink-log-*.csv")
        if not p.name.endswith(relink.DRY_RUN_LOG_SUFFIX)
    ]
    assert len(logs) == 1
    assert (
        relink.cmd_relink_undo(
            [
                "--data-dir",
                str(data_dir),
                "--log",
                str(logs[0]),
                "--live",
                "--i-understand-the-risks",
            ]
        )
        == 0
    )
    after = _rows(db)
    for pos, col in enumerate(relink.TRACK_COLUMNS):
        if col == "updated_at":
            continue
        assert after["a"][pos] == before["a"][pos], (
            f"if the CLI round trip does not restore {col} then --live is not "
            "safely reversible from its log alone"
        )


@pytest.mark.requirement("RECON-01")
def test_cli_undo_dry_run_reports_without_writing(tmp_path: Path) -> None:
    data_dir, disk, _target = _cli_data_dir(tmp_path)
    db = data_dir / "state" / "state.db"
    common = ["--data-dir", str(data_dir), "--roots", str(disk),
              "--index-cache", str(tmp_path / "c.json")]
    relink.cmd_relink([*common, "--live", "--i-understand-the-risks"])
    logs = [
        p
        for p in (data_dir / "reconcile").glob("relink-log-*.csv")
        if not p.name.endswith(relink.DRY_RUN_LOG_SUFFIX)
    ]
    digest = _sha256(db)
    code = relink.cmd_relink_undo(
        ["--data-dir", str(data_dir), "--log", str(logs[0])]
    )
    assert code == 0 and _sha256(db) == digest, (
        "if `relink-undo` without --live mutates state.db then dry-run is a lie"
    )


@pytest.mark.requirement("RECON-01")
def test_cli_nothing_to_apply_exits_one(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    _make_state_db(
        data_dir / "state" / "state.db",
        [{"stable_id": "a", "file_path": "/gone/nothing-like-this.mp3"}],
    )
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    code = relink.cmd_relink(
        [
            "--data-dir",
            str(data_dir),
            "--roots",
            str(empty_root),
            "--index-cache",
            str(tmp_path / "c.json"),
        ]
    )
    assert code == 1


@pytest.mark.requirement("RECON-01")
def test_dispatcher_lists_and_rejects_unknown_subcommands(capsys) -> None:
    assert reconcile_cli.main([]) == 0
    listed = capsys.readouterr().out
    for name in ("relink", "relink-undo", "relink-review"):
        assert name in listed
    assert reconcile_cli.main(["not-a-command"]) == 2


@pytest.mark.requirement("RECON-01")
def test_dispatcher_forwards_argv_to_the_subcommand(tmp_path: Path) -> None:
    data_dir, disk, _ = _cli_data_dir(tmp_path)
    code = reconcile_cli.main(
        [
            "relink",
            "--data-dir",
            str(data_dir),
            "--roots",
            str(disk),
            "--index-cache",
            str(tmp_path / "c.json"),
        ]
    )
    assert code == 0


@pytest.mark.requirement("RECON-01")
def test_dispatcher_refuses_arguments_for_legacy_no_arg_commands(capsys) -> None:
    assert reconcile_cli.main(["list-broken", "--nope"]) == 2
    assert "takes no arguments" in capsys.readouterr().err


# ----- tie-breakers -----------------------------------------------------


@pytest.mark.requirement("RECON-02")
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/Users/user/Music/Manual Library/x.mp3", "curated"),
        ("/Users/user/Music/Media.localized/Music/a/b/x.mp3", "curated"),
        ("/Users/user/Documents/Convert temp/TODO/x.mp3", "staging"),
        ("/Users/user/Music/Manual Library/Sep+ copy/x.mp3", "staging"),
        ("/Users/user/Downloads/x.mp3", "staging"),
        ("/Users/user/Music/Bass, House, Dance/x.mp3", "neutral"),
        # A curated-looking folder under the system temp dir is still staging:
        # an ancestor staging segment outranks the leaf, by design.
        ("/tmp/pytest-of-runner/pytest-0/t0/Manual Library/x.mp3", "staging"),
    ],
)
def test_path_classes_come_from_the_real_folder_names(path: str, expected: str) -> None:
    assert tiebreak.classify_path(path) == expected


@pytest.mark.requirement("RECON-02")
def test_ambient_temp_neutral_keeps_the_folder_name_rules(
    tmp_path: Path, ambient_temp_neutral: None,
) -> None:
    """if the neutral-ancestry fixture mutes folder-name rules then it hides bugs"""
    # Relative candidate strings must classify by exactly the folder names the
    # test spells out, with the full production vocabulary live. If the fixture
    # ever went back to editing STAGING_SEGMENTS, the second assertion here is
    # the one that would catch a lost folder-name rule.
    assert tiebreak.classify_path("x.mp3") == "neutral"
    assert tiebreak.classify_path("Manual Library/x.mp3") == "curated"
    assert tiebreak.classify_path("Convert temp/x.mp3") == "staging"
    # And the production globals really are untouched.
    assert "/tmp/" in tiebreak.STAGING_SEGMENTS
    assert "temp/" in tiebreak.STAGING_SEGMENTS


@pytest.mark.requirement("RECON-02")
def test_tmp_staging_rule_is_intact_without_the_neutraliser() -> None:
    """if "/tmp/" stops meaning staging then a real temp-dir copy outranks a library"""
    # The rule the neutral-ancestry fixture routes AROUND (via relative
    # paths). Deleting "/tmp/" from STAGING_SEGMENTS to make fixtures pass
    # would break real classification, so pin it here, outside the fixture.
    assert tiebreak.classify_path("/tmp/pytest-of-runner/p0/Manual Library/x.mp3") == "staging"


@pytest.mark.requirement("RECON-02")
def test_duration_agreement_ranks_a_candidate_first(tmp_path: Path) -> None:
    right = _audio(tmp_path, "dup.mp3")
    wrong = _audio(tmp_path / "other", "dup.mp3")
    index = index_disk.DiskIndex.build(
        [_disk_entry(str(right), duration=232.0), _disk_entry(str(wrong), duration=95.0)]
    )
    row = match.TrackRow(
        stable_id="a",
        title="Quiet Rooms",
        artist="Orla Finch",
        isrc=None,
        duration_ms=232000,
        file_path="/gone/dup.mp3",
    )
    res = _result(
        "a",
        bucket="relinkable-ambiguous",
        path="/gone/dup.mp3",
        candidates=[_cand(str(wrong), 0.90, "basename-duration"),
                    _cand(str(right), 0.90, "basename-duration")],
        ambiguity="multiple-above-threshold",
    )
    scored = tiebreak.score_candidates(row, res, index)
    assert scored.candidates[0].path == str(right), (
        "if duration agreement does not rank first then the tie-breaker adds no "
        "information over the matcher"
    )
    assert scored.candidates[0].duration_signal == tiebreak.W_DURATION_TIGHT


@pytest.mark.requirement("RECON-02")
def test_contradicted_isrc_ranks_below_a_candidate_with_no_isrc(tmp_path: Path) -> None:
    liar = _audio(tmp_path, "a.mp3")
    quiet = _audio(tmp_path / "other", "a.mp3")
    index = index_disk.DiskIndex.build(
        [_disk_entry(str(liar), isrc="ZZZZZ0000000"), _disk_entry(str(quiet))]
    )
    row = match.TrackRow(
        stable_id="a",
        title="T",
        artist="A",
        isrc="GBAYE2101234",
        duration_ms=None,
        file_path="/gone/a.mp3",
    )
    res = _result(
        "a",
        bucket="relinkable-ambiguous",
        path="/gone/a.mp3",
        candidates=[_cand(str(liar), 0.90), _cand(str(quiet), 0.90)],
        ambiguity="multiple-above-threshold",
    )
    scored = tiebreak.score_candidates(row, res, index)
    assert scored.candidates[0].path == str(quiet)
    assert scored.candidates[-1].isrc_signal == tiebreak.W_ISRC_CONTRADICTED


@pytest.mark.requirement("RECON-02")
def test_folder_class_alone_never_resolves_a_row(
    tmp_path: Path, ambient_temp_neutral: None,
) -> None:
    # Real files, addressed RELATIVE to tmp_path (the fixture's cwd) so the
    # classified string carries only the folder names this test spells out.
    _audio(tmp_path / "Manual Library", "dup.mp3")
    _audio(tmp_path / "Convert temp", "dup.mp3")
    curated = "Manual Library/dup.mp3"
    staging = "Convert temp/dup.mp3"
    index = index_disk.DiskIndex.build(
        [_disk_entry(curated, duration=232.0), _disk_entry(staging, duration=232.0)]
    )
    row = match.TrackRow(
        stable_id="a", title="T", artist="A", isrc=None, duration_ms=232000,
        file_path="/gone/dup.mp3",
    )
    res = _result(
        "a",
        bucket="relinkable-ambiguous",
        path="/gone/dup.mp3",
        candidates=[_cand(curated, 0.90, "basename-duration"),
                    _cand(staging, 0.90, "basename-duration")],
        ambiguity="multiple-above-threshold",
    )
    scored = tiebreak.score_rows([row], [res], index)[0]
    # Both candidates must SURVIVE the hard filters, otherwise _verdict
    # short-circuits on "no candidate survives" and never reaches the margin
    # rule this test exists to pin. Assert that explicitly rather than trust it.
    assert [c.hard_reject for c in scored.candidates] == ["", ""]
    assert scored.candidates[0].path == curated
    assert not scored.resolved, (
        "if folder plausibility alone resolves a row then two identical-duration "
        "files can be separated on taste and applied"
    )
    assert scored.resolved_reason and "under" in scored.resolved_reason
    assert tiebreak.RESOLVE_MARGIN > tiebreak.PATH_SPAN


@pytest.mark.requirement("RECON-02")
def test_content_signals_resolve_a_single_weak_candidate(tmp_path: Path) -> None:
    only = _audio(tmp_path / "Manual Library", "renamed.mp3")
    index = index_disk.DiskIndex.build(
        [_disk_entry(str(only), duration=232.0, size=4242, isrc="GBAYE2101234")]
    )
    row = match.TrackRow(
        stable_id="a",
        title="Quiet Rooms",
        artist="Orla Finch",
        isrc="GBAYE2101234",
        duration_ms=232000,
        file_path="/gone/02 Quiet Rooms.mp3",
        expected_size_bytes=4242,
    )
    res = _result(
        "a",
        bucket="relinkable-ambiguous",
        path="/gone/02 Quiet Rooms.mp3",
        candidates=[_cand(str(only), 0.78, "title-artist-fuzzy")],
        ambiguity="below-auto-threshold",
    )
    scored = tiebreak.score_rows([row], [res], index)[0]
    assert scored.resolved, (
        "if duration + size + ISRC agreement cannot resolve a lone fuzzy "
        "candidate then the tie-breakers can never recover anything"
    )
    assert scored.top is not None and scored.top.score >= tiebreak.RESOLVE_SCORE_MIN


@pytest.mark.requirement("RECON-02")
def test_target_already_linked_is_never_resolved(tmp_path: Path) -> None:
    taken = _audio(tmp_path, "taken.mp3")
    index = index_disk.DiskIndex.build(
        [_disk_entry(str(taken), duration=232.0, size=1, isrc="GBAYE2101234")]
    )
    row = match.TrackRow(
        stable_id="a", title="T", artist="A", isrc="GBAYE2101234", duration_ms=232000,
        file_path="/gone/taken.mp3", expected_size_bytes=1,
    )
    res = _result(
        "a", bucket="relinkable-ambiguous", path="/gone/taken.mp3",
        candidates=[_cand(str(taken), 0.95)], ambiguity="target-already-linked",
    )
    other = _result("b", bucket="present", path=str(taken))
    scored = tiebreak.score_rows(
        [row, match.TrackRow("b", None, None, None, None, str(taken))], [res, other], index
    )[0]
    assert not scored.resolved
    assert "duplicate decision" in scored.resolved_reason
    assert scored.top is None, (
        "if a taken path is still rank 1 then a reviewer can accept a duplicate"
    )


@pytest.mark.requirement("RECON-02")
def test_a_contest_is_awarded_to_the_best_scoring_claimant(tmp_path: Path) -> None:
    target = _audio(tmp_path / "Manual Library", "shared.mp3")
    index = index_disk.DiskIndex.build(
        [_disk_entry(str(target), duration=232.0, size=4242, isrc="GBAYE2101234")]
    )
    winner = match.TrackRow(
        stable_id="win", title="T", artist="A", isrc="GBAYE2101234",
        duration_ms=232000, file_path="/gone/one/shared.mp3", expected_size_bytes=4242,
    )
    loser = match.TrackRow(
        stable_id="lose", title="T", artist="A", isrc=None,
        duration_ms=900000, file_path="/gone/two/shared.mp3",
    )
    results = [
        _result("win", bucket="relinkable-ambiguous", path="/gone/one/shared.mp3",
                candidates=[_cand(str(target), 0.95)], ambiguity="target-contested"),
        _result("lose", bucket="relinkable-ambiguous", path="/gone/two/shared.mp3",
                candidates=[_cand(str(target), 0.95)], ambiguity="target-contested"),
    ]
    scored = tiebreak.score_rows([winner, loser], results, index)
    by_id = {r.stable_id: r for r in scored}
    assert by_id["win"].resolved and not by_id["lose"].resolved, (
        "if a contest cannot be awarded then 22 real rows stay stuck forever"
    )
    assert "wins a 2-row contest" in by_id["win"].resolved_reason


@pytest.mark.requirement("RECON-02")
def test_review_csv_ranks_candidates_and_shares_one_verdict(
    tmp_path: Path, ambient_temp_neutral: None,
) -> None:
    _audio(tmp_path / "Manual Library", "dup.mp3")
    _audio(tmp_path / "Convert temp", "dup.mp3")
    a = "Manual Library/dup.mp3"
    b = "Convert temp/dup.mp3"
    index = index_disk.DiskIndex.build(
        [_disk_entry(a, duration=232.0), _disk_entry(b, duration=232.0)]
    )
    row = match.TrackRow("a", "T", "A", None, 232000, "/gone/dup.mp3")
    res = _result(
        "a", bucket="relinkable-ambiguous", path="/gone/dup.mp3",
        candidates=[_cand(a, 0.90, "basename-duration"), _cand(b, 0.90, "basename-duration")],
        ambiguity="multiple-above-threshold",
    )
    scored = tiebreak.score_rows([row], [res], index)
    out = tiebreak.write_review_csv(scored, tiebreak.review_path_for(tmp_path, date="2026-07-28"))
    assert out.name == "relink-review-2026-07-28.csv"
    lines = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [int(line["rank"]) for line in lines] == [1, 2]
    assert {line["resolved"] for line in lines} == {"no"}
    # "no" must mean "the margin rule refused it", not "both candidates were
    # hard-rejected as missing" -- the latter passes this assertion for the
    # wrong reason and makes the whole CSV unrepresentative of a real review.
    assert {line["resolved_reason"] for line in lines} == {
        line["resolved_reason"] for line in lines if "under" in line["resolved_reason"]
    }
    assert lines[0]["path_class"] == "curated" and lines[1]["path_class"] == "staging"
    assert set(lines[0]) == set(tiebreak.REVIEW_COLUMNS)


@pytest.mark.requirement("RECON-02")
def test_review_cli_leaves_state_db_untouched(tmp_path: Path) -> None:
    data_dir, disk, _ = _cli_data_dir(tmp_path)
    db = data_dir / "state" / "state.db"
    digest = _sha256(db)
    code = tiebreak.main(
        [
            "--data-dir",
            str(data_dir),
            "--roots",
            str(disk),
            "--index-cache",
            str(tmp_path / "c.json"),
            "--out",
            str(tmp_path / "review.csv"),
        ]
    )
    assert code == 0 and _sha256(db) == digest


# ----- real library (scratch copy only) --------------------------------


def _real_state_db() -> Path:
    env = os.environ.get("MDT_DATA_DIR")
    from apps.shared import paths as shared_paths

    base = Path(env) if env else shared_paths.DATA_DIR
    return base / "state" / "state.db"


@pytest.mark.slow
@pytest.mark.skipif(
    not HAS_REAL_LIBRARY,
    reason="real library not present (state.db missing, unreadable, or has zero track rows)",
)
@pytest.mark.requirement("RECON-01")
def test_undo_on_a_scratch_copy_of_the_real_db_restores_byte_identical_rows(
    tmp_path: Path,
) -> None:
    """The proof the maintainer asked for, as a permanent regression test.

    Copies the real state.db, applies the first few auto relinks derived from the
    live plan, undoes them, and asserts every row value is byte-identical
    (``updated_at`` excepted, which is the writer's own stamp).
    """
    scratch = tmp_path / "state.db"
    shutil.copy2(_real_state_db(), scratch)
    conn = sqlite3.connect(f"file:{scratch}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT stable_id, file_path FROM tracks WHERE file_path IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    # Build entries from real rows, retargeted at throwaway files so the test
    # never depends on the current disk layout.
    entries: list[relink.RelinkEntry] = []
    for sid, old in rows[:5]:
        target = _audio(tmp_path / "disk", f"{sid[:8]}.mp3")
        entries.append(_entry(sid, old, str(target)))
    before = _rows(scratch)
    relink.apply_entries(scratch, entries)
    mid = _rows(scratch)
    idx = relink.TRACK_COLUMNS.index("file_path")
    assert [mid[e.stable_id][idx] for e in entries] == [e.new_path for e in entries]
    relink.apply_entries(scratch, entries, reverse=True, tolerate_conflicts=True)
    after = _rows(scratch)
    assert set(after) == set(before)
    for sid in before:
        for pos, col in enumerate(relink.TRACK_COLUMNS):
            if col == "updated_at":
                continue
            assert after[sid][pos] == before[sid][pos], (
                f"if undo does not restore {col} on the real schema then the "
                "live run is not reversible"
            )
    assert json.loads(json.dumps(list(after))) == json.loads(json.dumps(list(before)))
