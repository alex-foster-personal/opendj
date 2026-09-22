"""Tests for ``apps.dedup.apply``."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from apps.dedup import apply as apply_mod
from apps.dedup import schema as dedup_schema

# This module exercises live-write MECHANICS against tmp fixtures, so it runs
# with the one-way rekordbox import gate ON (root conftest reads the marker).
# It never touches a real rekordbox target.
pytestmark = pytest.mark.rekordbox_writeback


def _seed_cluster(dedup_db: Path, *, canonical: str, alias: str, cluster_id: int = 1) -> None:
    conn = dedup_schema.ensure_schema(dedup_db)
    try:
        conn.execute(
            "INSERT INTO duplicate_clusters (cluster_id, canonical_stable_id, canonical_path, rationale) VALUES (?, ?, ?, ?)",
            (cluster_id, "cano", canonical, "test"),
        )
        conn.execute(
            "INSERT INTO track_aliases (alias_stable_id, alias_path, cluster_id, canonical_stable_id, similarity) VALUES (?, ?, ?, ?, ?)",
            ("alia", alias, cluster_id, "cano", 0.95),
        )
        conn.commit()
    finally:
        conn.close()


def _peek(db: Path, cid: str) -> str | None:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT FolderPath FROM djmdContent WHERE ID = ?", (cid,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.requirement("META-03")
def test_dry_run_no_db_mutation(tmp_path: Path, tmp_rb_db: Path) -> None:
    dedup_db = tmp_path / "phase7.sqlite"
    conn = sqlite3.connect(tmp_rb_db)
    row = conn.execute("SELECT ID, FolderPath FROM djmdContent LIMIT 1").fetchone()
    conn.close()
    if row is None:
        pytest.skip("rb fixture empty")
    _rb_id, rb_path = row
    _seed_cluster(dedup_db, canonical="/canonical/path.mp3", alias=rb_path)

    before = _sha(tmp_rb_db)
    res = apply_mod.run_apply(
        apply_paths=apply_mod.DedupApplyPaths(
            dedup_db_path=dedup_db,
            rb_db_path=tmp_rb_db,
            plan_csv=tmp_path / "plan.csv",
            plan_md=tmp_path / "plan.md",
        ),
        live=False,
    )
    assert _sha(tmp_rb_db) == before, "dry-run mutated DB"
    assert res["dry_run"] is True
    assert res["plan_rows"] == 1
    assert res["with_rb"] == 1
    assert (tmp_path / "plan.csv").exists()
    assert (tmp_path / "plan.md").exists()


@pytest.mark.requirement("META-03")
def test_dry_run_plan_csv_shape(tmp_path: Path, tmp_rb_db: Path) -> None:
    dedup_db = tmp_path / "phase7.sqlite"
    _seed_cluster(dedup_db, canonical="/canon.mp3", alias="/alias-no-match.mp3")
    apply_mod.run_apply(
        apply_paths=apply_mod.DedupApplyPaths(
            dedup_db_path=dedup_db,
            rb_db_path=tmp_rb_db,
            plan_csv=tmp_path / "plan.csv",
            plan_md=tmp_path / "plan.md",
        ),
        live=False,
    )
    import csv

    with open(tmp_path / "plan.csv", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert header == [
        "cluster_id",
        "canonical_path",
        "alias_path",
        "rb_content_ids",
        "playlist_names",
    ]


@pytest.mark.requirement("META-03")
def test_cautious_live_rewrites_one_row(
    tmp_path: Path, tmp_rb_db: Path, monkeypatch
) -> None:
    import pyrekordbox.db6.database as rb_db_mod

    monkeypatch.setattr(rb_db_mod, "get_rekordbox_pid", lambda: 0)

    dedup_db = tmp_path / "phase7.sqlite"
    conn = sqlite3.connect(tmp_rb_db)
    row = conn.execute("SELECT ID, FolderPath FROM djmdContent LIMIT 1").fetchone()
    conn.close()
    if row is None:
        pytest.skip("rb fixture empty")
    rb_id, rb_path = row
    canonical = str(tmp_path / "canonical" / "file.mp3")
    _seed_cluster(dedup_db, canonical=canonical, alias=rb_path)

    res = apply_mod.run_apply(
        apply_paths=apply_mod.DedupApplyPaths(
            dedup_db_path=dedup_db,
            rb_db_path=tmp_rb_db,
            plan_csv=tmp_path / "plan.csv",
            plan_md=tmp_path / "plan.md",
            backup_dir=tmp_path / "backups",
        ),
        live=True,
        allow_rb_running=True,
        confirm_fn=lambda: True,
    )

    assert res["applied"] == 1, f"expected 1 applied, got {res}"
    assert res["verified"] is True
    assert res["backup"] is not None
    assert res["reversal"] is not None
    assert _peek(tmp_rb_db, str(rb_id)) == canonical


@pytest.mark.requirement("META-03")
def test_abort_if_rb_running(tmp_path: Path, tmp_rb_db: Path, monkeypatch) -> None:
    monkeypatch.setattr(apply_mod, "_rekordbox_running", lambda: True)
    dedup_db = tmp_path / "phase7.sqlite"
    _seed_cluster(dedup_db, canonical="/a.mp3", alias="/b.mp3")
    before = _sha(tmp_rb_db)

    res = apply_mod.run_apply(
        apply_paths=apply_mod.DedupApplyPaths(
            dedup_db_path=dedup_db,
            rb_db_path=tmp_rb_db,
            plan_csv=tmp_path / "plan.csv",
            plan_md=tmp_path / "plan.md",
            backup_dir=tmp_path / "backups",
        ),
        live=True,
        allow_rb_running=False,
    )
    assert res["applied"] == 0
    assert any("Rekordbox is running" in e for e in res["errors"])
    assert _sha(tmp_rb_db) == before


@pytest.mark.requirement("META-03")
def test_confirm_refused_aborts(tmp_path: Path, tmp_rb_db: Path, monkeypatch) -> None:
    import pyrekordbox.db6.database as rb_db_mod

    monkeypatch.setattr(rb_db_mod, "get_rekordbox_pid", lambda: 0)
    dedup_db = tmp_path / "phase7.sqlite"
    conn = sqlite3.connect(tmp_rb_db)
    row = conn.execute("SELECT ID, FolderPath FROM djmdContent LIMIT 1").fetchone()
    conn.close()
    if row is None:
        pytest.skip("rb fixture empty")
    _rb_id, rb_path = row
    _seed_cluster(dedup_db, canonical="/canon.mp3", alias=rb_path)

    before = _sha(tmp_rb_db)
    res = apply_mod.run_apply(
        apply_paths=apply_mod.DedupApplyPaths(
            dedup_db_path=dedup_db,
            rb_db_path=tmp_rb_db,
            plan_csv=tmp_path / "plan.csv",
            plan_md=tmp_path / "plan.md",
            backup_dir=tmp_path / "backups",
        ),
        live=True,
        allow_rb_running=True,
        confirm_fn=lambda: False,
    )
    assert res["applied"] == 0
    assert any("Confirmation" in e for e in res["errors"])
    assert _sha(tmp_rb_db) == before


@pytest.mark.requirement("META-03")
def test_reversal_script_generated(
    tmp_path: Path, tmp_rb_db: Path, monkeypatch
) -> None:
    import pyrekordbox.db6.database as rb_db_mod

    monkeypatch.setattr(rb_db_mod, "get_rekordbox_pid", lambda: 0)
    dedup_db = tmp_path / "phase7.sqlite"
    conn = sqlite3.connect(tmp_rb_db)
    row = conn.execute("SELECT ID, FolderPath FROM djmdContent LIMIT 1").fetchone()
    conn.close()
    if row is None:
        pytest.skip("rb fixture empty")
    _rb_id, rb_path = row
    _seed_cluster(dedup_db, canonical="/canon.mp3", alias=rb_path)

    backup_dir = tmp_path / "backups"
    res = apply_mod.run_apply(
        apply_paths=apply_mod.DedupApplyPaths(
            dedup_db_path=dedup_db,
            rb_db_path=tmp_rb_db,
            plan_csv=tmp_path / "plan.csv",
            plan_md=tmp_path / "plan.md",
            backup_dir=backup_dir,
        ),
        live=True,
        allow_rb_running=True,
        confirm_fn=lambda: True,
    )
    reversal = Path(res["reversal"])
    assert reversal.exists()
    assert "cp -v" in reversal.read_text()
    assert str(tmp_rb_db) in reversal.read_text()


@pytest.mark.requirement("META-03")
def test_cli_live_requires_i_understand(tmp_path: Path, tmp_rb_db: Path, capsys) -> None:
    dedup_db = tmp_path / "phase7.sqlite"
    _seed_cluster(dedup_db, canonical="/a.mp3", alias="/b.mp3")
    rc = apply_mod.main(
        [
            "--db", str(dedup_db),
            "--rb-db", str(tmp_rb_db),
            "--plan-csv", str(tmp_path / "plan.csv"),
            "--plan-md", str(tmp_path / "plan.md"),
            "--live",
        ]
    )
    assert rc == 2
    assert "i-understand-the-risks" in capsys.readouterr().err


@pytest.mark.requirement("META-03")
def test_cli_dry_run_smoke(tmp_path: Path, tmp_rb_db: Path, capsys) -> None:
    dedup_db = tmp_path / "phase7.sqlite"
    _seed_cluster(dedup_db, canonical="/a.mp3", alias="/b.mp3")
    rc = apply_mod.main(
        [
            "--db", str(dedup_db),
            "--rb-db", str(tmp_rb_db),
            "--plan-csv", str(tmp_path / "plan.csv"),
            "--plan-md", str(tmp_path / "plan.md"),
        ]
    )
    assert rc == 0
    assert "dry_run=True" in capsys.readouterr().out


@pytest.mark.requirement("META-03")
def test_reversal_script_written_before_write(
    tmp_path: Path, tmp_rb_db: Path, monkeypatch
) -> None:
    """P0 regression (adversarial #3, HIGH): the reversal script MUST
    exist on disk BEFORE ``_apply_rewrites_live`` runs. Pre-fix the
    script was only emitted after verify, so a crash mid-write left
    the backup without a recovery path.
    """
    import pyrekordbox.db6.database as rb_db_mod

    monkeypatch.setattr(rb_db_mod, "get_rekordbox_pid", lambda: 0)

    dedup_db = tmp_path / "phase7.sqlite"
    conn = sqlite3.connect(tmp_rb_db)
    row = conn.execute(
        "SELECT ID, FolderPath FROM djmdContent LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        pytest.skip("rb fixture empty")
    _rb_id, rb_path = row
    _seed_cluster(dedup_db, canonical="/canon.mp3", alias=rb_path)

    backup_dir = tmp_path / "backups"

    # Snapshot the reversal scripts that exist just before the write
    # runs. We intercept ``_apply_rewrites_live`` and raise from inside
    # it -- at that moment, the reversal script MUST already be on disk.
    reversal_snapshot: dict[str, list[Path]] = {"seen_at_write_time": []}

    original_apply = apply_mod._apply_rewrites_live

    def _exploding_apply(*args, **kwargs):
        # Capture every reversal-script filename visible to the
        # operator at the instant the live write begins. This is what
        # the adversarial review requires: a crash at this point must
        # leave a reversal path behind.
        reversal_snapshot["seen_at_write_time"] = sorted(
            backup_dir.glob("reverse-dedup-*.sh")
        )
        raise RuntimeError("simulated mid-write crash")

    monkeypatch.setattr(apply_mod, "_apply_rewrites_live", _exploding_apply)

    with pytest.raises(RuntimeError, match="simulated mid-write crash"):
        apply_mod.run_apply(
            apply_paths=apply_mod.DedupApplyPaths(
                dedup_db_path=dedup_db,
                rb_db_path=tmp_rb_db,
                plan_csv=tmp_path / "plan.csv",
                plan_md=tmp_path / "plan.md",
                backup_dir=backup_dir,
            ),
            live=True,
            allow_rb_running=True,
            confirm_fn=lambda: True,
        )

    # Rail 6 hardening: at the moment the write began, a reversal
    # script was already on disk. The post-crash filesystem therefore
    # gives the operator a recovery path.
    scripts_at_crash = reversal_snapshot["seen_at_write_time"]
    assert len(scripts_at_crash) == 1, (
        f"expected reversal script to exist BEFORE write; found {scripts_at_crash}"
    )
    script = scripts_at_crash[0]
    body = script.read_text()
    assert "cp -v" in body
    assert str(tmp_rb_db) in body

    # Also survives the crash (sanity check: file is still there after).
    after_crash = sorted(backup_dir.glob("reverse-dedup-*.sh"))
    assert after_crash == scripts_at_crash

    # Keep the unused original binding silenced; we intentionally did
    # not call it (the monkeypatch replaces it for the duration).
    del original_apply
