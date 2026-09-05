"""CLI smoke tests: agent-native parity means every flow has a command line.

[if] a documented mik CLI subcommand runs, live or dry [then] it exits 0 as documented, [else stop].
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.mik import cli
from apps.mik import load as loader
from apps.mik import promote as promoter
from apps.shared.mik_energy import readable_mik_energy
from apps.shared.state import provenance as _prov

pytestmark = pytest.mark.requirement("META-01")


@pytest.fixture
def library(state_conn: sqlite3.Connection, add_track, make_mik_store, tmp_path: Path):
    add_track("a" * 40, file_path="/Users/dev/x.mp3", title="X", artist="Y")
    state_conn.commit()
    return make_mik_store(
        [
            {"path": "/Users/dev/x.mp3", "confidence": 0.9, "segments": [(0.0, 60.0, 5)]},
            {"path": "/none/y.mp3", "confidence": 0.9, "segments": [(0.0, 30.0, 3)]},
        ]
    )


def _run(argv: list[str], capsys) -> dict:
    assert cli.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_read_reports_what_mik_holds(library: Path, capsys) -> None:
    payload = _run(["read", "--json", "--store", str(library)], capsys)
    assert payload["songs"] == 2
    assert payload["segment_rows"] == 2


def test_coverage_records_the_readable_mik_energy_fraction(
    library: Path, data_dir: Path, add_track, capsys
) -> None:
    add_track("b" * 40, file_path="/Users/dev/unmatched.mp3", title="Unmatched", artist="DJ")
    payload = _run(
        ["coverage", "--json", "--data-dir", str(data_dir), "--store", str(library)], capsys
    )
    assert payload["tracks_indexed"] == 2
    assert payload["tracks_matched"] == 1
    assert payload["tracks_with_readable_mik_energy"] == 1
    assert payload["fraction"] == 1.0
    assert payload["mode"] == "read-only"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, None), (7.8, None), ("7", None), (0, None), (10, None), (1, 1), (9, 9), (7.0, 7)],
)
def test_readable_mik_energy_matches_the_performance_display_scale(
    value: object, expected: int | None
) -> None:
    assert readable_mik_energy(value) == expected


def test_availability_dry_run_writes_nothing(library: Path, data_dir: Path, capsys) -> None:
    payload = _run(["availability", "--json", "--data-dir", str(data_dir)], capsys)
    assert payload["mode"] == "dry-run"
    assert payload["written"] == {"changed": 0, "unchanged": 0}
    assert payload["stored"]["unknown"] == 1


def test_availability_live_then_idempotent(library: Path, data_dir: Path, capsys) -> None:
    first = _run(["availability", "--json", "--live", "--data-dir", str(data_dir)], capsys)
    assert first["written"]["changed"] == 1
    second = _run(["availability", "--json", "--live", "--data-dir", str(data_dir)], capsys)
    assert second["written"] == {"changed": 0, "unchanged": 1}


def test_match_reports_tiers(library: Path, data_dir: Path, capsys) -> None:
    payload = _run(
        ["match", "--json", "--data-dir", str(data_dir), "--store", str(library)],
        capsys,
    )
    assert payload["matched"] == 1
    assert payload["by_tier"]["exact_path"] == 1
    assert payload["by_reason"] == {"no_candidate": 1}


def test_load_without_a_verdict_file_plans_nothing(library: Path, data_dir: Path, capsys) -> None:
    payload = _run(
        ["load", "--json", "--data-dir", str(data_dir), "--store", str(library)],
        capsys,
    )
    assert payload["verdict_file_present"] is False
    assert payload["planned"] == {
        "field_writes": 0,
        "segment_tracks": 0,
        "segment_rows": 0,
        "staged_rows": 0,
    }
    assert set(payload["equivalence"].values()) == {"untested"}


def test_load_live_splits_matched_and_unmatched(
    library: Path, data_dir: Path, write_verdicts, capsys
) -> None:
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    payload = _run(
        [
            "load",
            "--json",
            "--live",
            "--data-dir",
            str(data_dir),
            "--store",
            str(library),
        ],
        capsys,
    )
    assert payload["applied"]["fields_written"] == len(loader.SCALAR_FIELDS)
    assert payload["applied"]["segment_rows"] == 1
    assert payload["applied"]["staged_written"] == len(loader.GATED_FIELDS)


def test_exact_path_only_narrows_matching(library: Path, data_dir: Path, capsys) -> None:
    payload = _run(
        [
            "match",
            "--json",
            "--exact-path-only",
            "--data-dir",
            str(data_dir),
            "--store",
            str(library),
        ],
        capsys,
    )
    assert payload["allow_fuzzy"] is False


def test_match_and_dry_run_load_open_state_db_read_only(
    library: Path, data_dir: Path, capsys, monkeypatch
) -> None:
    """P2 regression (PR #383 review): ``match`` is documented read-only and
    ``load`` is dry-run by default, but ``_read_and_match`` always opened the
    state DB with ``open_rw``, which applies pending schema migrations and
    the machine-ID backfill before any report is produced -- silently
    mutating ``state.db`` despite the command's own contract.

    Superseded, same evidence updated (P1 regression, apps/mik/cli.py:112):
    the dry-run branch no longer calls ``open_ro`` directly either -- it
    calls ``open_dry_run`` (a migrated, disposable sibling copy), because a
    plain read-only handle to an un-migrated real database raises
    ``no such table`` the moment dry-run code touches a v8-only table. Either
    way, the invariant this test protects is unchanged: a dry run must never
    reach ``open_rw`` and mutate the real file."""
    calls: list[str] = []
    real_dry_run = cli.state_db.open_dry_run
    real_rw = cli.state_db.open_rw

    def _spy_dry_run(*a, **k):
        calls.append("dry_run")
        return real_dry_run(*a, **k)

    def _spy_rw(*a, **k):
        calls.append("rw")
        return real_rw(*a, **k)

    monkeypatch.setattr(cli.state_db, "open_dry_run", _spy_dry_run)
    monkeypatch.setattr(cli.state_db, "open_rw", _spy_rw)

    _run(
        ["match", "--json", "--data-dir", str(data_dir), "--store", str(library)],
        capsys,
    )
    assert calls == ["dry_run"]

    calls.clear()
    _run(
        ["load", "--json", "--data-dir", str(data_dir), "--store", str(library)],
        capsys,
    )
    assert calls == ["dry_run"]

    calls.clear()
    _run(
        [
            "load",
            "--json",
            "--live",
            "--data-dir",
            str(data_dir),
            "--store",
            str(library),
        ],
        capsys,
    )
    assert calls == ["rw"]  # positive control: a live load still needs write access

    # P2 regression, fresh evidence: availability and promote bypass
    # _read_and_match entirely and each had their own unconditional
    # open_rw, so the same dry-run leak applied to them independently.
    calls.clear()
    _run(["availability", "--json", "--data-dir", str(data_dir)], capsys)
    assert calls == ["dry_run"]

    calls.clear()
    _run(["availability", "--json", "--live", "--data-dir", str(data_dir)], capsys)
    assert calls == ["rw"]  # positive control

    calls.clear()
    _run(["promote", "--json", "--discover", "--data-dir", str(data_dir)], capsys)
    assert calls == ["dry_run"]


def test_promote_needs_a_target(library: Path, data_dir: Path) -> None:
    with pytest.raises(SystemExit, match="--discover"):
        cli.main(["promote", "--data-dir", str(data_dir)])


def test_promote_discover_dry_run(library: Path, data_dir: Path, write_verdicts, capsys) -> None:
    write_verdicts({"energy": "passed"})
    cli.main(
        [
            "load",
            "--json",
            "--live",
            "--data-dir",
            str(data_dir),
            "--store",
            str(library),
        ]
    )
    capsys.readouterr()
    payload = _run(["promote", "--json", "--discover", "--data-dir", str(data_dir)], capsys)
    assert payload["mode"] == "dry-run"
    assert payload["pending_by_reason"] == {"no_candidate": 1}
    assert payload["applied"] is None


def test_promote_forwards_the_overwrite_opt_in(
    library: Path,
    data_dir: Path,
    state_conn: sqlite3.Connection,
    add_track,
    write_verdicts,
    capsys,
) -> None:
    """P2 regression (PR #383 review): for
    ``python -m apps.mik promote --live --overwrite-lower-precedence``,
    argparse sets the advertised option, but cmd_promote must forward it to
    promote_all the same way cmd_load already does, or it silently reverts to
    the default False and a lower-precedence value never gets overwritten."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    cli.main(["load", "--json", "--live", "--data-dir", str(data_dir), "--store", str(library)])
    capsys.readouterr()
    pending = promoter.staged_rows(state_conn, pending_only=True)
    source_row_id = pending[0].source_row_id
    stable_id = add_track("b" * 40, file_path="/other/y.mp3")
    _prov.write_field(
        state_conn,
        stable_id=stable_id,
        field_name="energy",
        value=3,
        source="rekordbox",
        modified_at="2024-01-01T00:00:00+00:00",
        actor="test",
        now="2024-01-01T00:00:00+00:00",
    )
    _run(
        [
            "promote",
            "--json",
            "--live",
            "--overwrite-lower-precedence",
            "--source-row-id",
            source_row_id,
            "--stable-id",
            stable_id,
            "--data-dir",
            str(data_dir),
        ],
        capsys,
    )
    written_source = state_conn.execute(
        "SELECT source FROM track_fields WHERE stable_id = ? AND field_name = ?",
        (stable_id, "energy"),
    ).fetchone()[0]
    assert written_source == "mik"


def test_every_subcommand_accepts_json_and_data_dir() -> None:
    parser = cli.build_parser()
    for command in ("availability", "read", "match", "coverage", "load", "promote"):
        args = parser.parse_args([command, "--json", "--data-dir", "/tmp"])
        assert args.json is True
        assert args.data_dir == "/tmp"
        assert args.live is False  # dry-run is always the default


# --------------------------------------- dry runs against a pre-v8 database


def _make_v7_state_db(path: Path) -> None:
    """A real state.db stamped at schema v7, missing every v8 table.

    Built by running only ``MIGRATIONS[:7]`` and recording ``schema_meta``
    at 7 by hand, so it is byte-for-byte what a live agentbox/bifrost2
    database looked like before this branch's v8 migration existed --
    exactly the shape review thread apps/mik/cli.py:112 says a documented
    default dry run must not choke on.
    """
    from apps.shared.state import schema as _schema

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("BEGIN")
        for statements in _schema.MIGRATIONS[:7]:
            for stmt in statements:
                conn.execute(stmt)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (7, ?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _schema_version(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_meta").fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_availability_dry_run_on_v7_db_does_not_raise_and_does_not_migrate(
    tmp_path: Path, capsys
) -> None:
    """The documented default (``availability`` with no ``--live``) must work
    on every existing v7 database, not raise ``no such table:
    track_availability`` -- and must leave that real, pre-v8 file untouched,
    since a dry run previewing an operation is not licensed to upgrade it.
    """
    data_dir = tmp_path
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "state" / "state.db"
    _make_v7_state_db(db_path)

    payload = _run(["availability", "--json", "--data-dir", str(data_dir)], capsys)

    assert payload["mode"] == "dry-run"
    assert payload["written"] == {"changed": 0, "unchanged": 0}
    # The real file on disk was never migrated by the dry run.
    assert _schema_version(db_path) == 7


def test_load_dry_run_on_v7_db_does_not_raise(tmp_path: Path, capsys) -> None:
    """``load``'s dry-run path builds a plan that queries the v8-only
    ``unmatched_source_analysis`` table even before ``--live``; it must not
    require the real database to already be migrated to preview that.
    """
    data_dir = tmp_path
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "state" / "state.db"
    _make_v7_state_db(db_path)
    store = tmp_path / "empty.mikdb"
    conn = sqlite3.connect(store)
    conn.executescript(
        """
        CREATE TABLE ZSONG (
            Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZARTIST TEXT, ZALBUM TEXT,
            ZKEY TEXT, ZENERGY REAL, ZTEMPO REAL, ZVOLUME REAL,
            ZCLIPPEDPEAKCOUNT INTEGER, ZANALYSISDATE REAL, ZBOOKMARKDATA BLOB
        );
        CREATE TABLE ZENERGYSEGMENT (
            Z_PK INTEGER PRIMARY KEY, ZSONG INTEGER, ZENERGY REAL,
            ZLENGTH REAL, ZSTARTTIME REAL
        );
        CREATE TABLE ZKEYSEGMENT (
            Z_PK INTEGER PRIMARY KEY, ZSONG INTEGER, ZCONFIDENCE REAL, ZKEY TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    payload = _run(
        [
            "load",
            "--json",
            "--data-dir",
            str(data_dir),
            "--store",
            str(store),
        ],
        capsys,
    )

    assert payload["mode"] == "dry-run"
    # The real file on disk was never migrated by the dry run.
    assert _schema_version(db_path) == 7
