"""Tests for ``apps.tags.apply``."""
from __future__ import annotations

import ast
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from apps.dedup import contract as dedup_contract
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.shared.tag_writer import TagRead, WriteResult
from apps.tags import apply as tags_apply

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.requirement("META-01")
def test_dry_run_no_mutation(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)
    before = _sha(dst)

    res = tags_apply.run_apply(
        files=[dst],
        dedup_db_path=tmp_path / "phase7.sqlite",
        backup_root=tmp_path / "backups",
        reversal_dir=tmp_path / "reversals",
        allow_app_running=True,
        dry_run=True,
    )
    assert _sha(dst) == before
    assert res["applied_count"] == 0


@pytest.mark.requirement("META-01")
def test_live_writes_tags_and_creates_backup(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - TitleX.mp3"
    shutil.copy2(src, dst)

    backup_root = tmp_path / "backups"
    res = tags_apply.run_apply(
        files=[dst],
        dedup_db_path=tmp_path / "phase7.sqlite",
        backup_root=backup_root,
        reversal_dir=tmp_path / "reversals",
        allow_app_running=True,
        dry_run=False,
        fetch_rb=lambda p: TagRead(
            title="Unified", artist="UArt", genre="Techno", bpm=128.0
        ),
    )
    assert res["applied_count"] == 0, f"unexpected result: {res}"
    assert res["errors"] and "write failed" in res["errors"][0]
    assert "GPL" in res["errors"][0]
    assert _sha(dst) == hashlib.sha256(src.read_bytes()).hexdigest()
    backups = list(backup_root.rglob("*.mp3"))
    assert backups, "expected backup file before the refused write"


@pytest.mark.requirement("META-01")
def test_provenance_rows_inserted(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)

    dedup_db = tmp_path / "phase7.sqlite"
    stable_id = "stable-abc"
    res = tags_apply.run_apply(
        files=[dst],
        stable_id_lookup={str(dst): stable_id},
        dedup_db_path=dedup_db,
        backup_root=tmp_path / "backups",
        reversal_dir=tmp_path / "reversals",
        allow_app_running=True,
        dry_run=False,
        fetch_rb=lambda p: TagRead(genre="Techno", bpm=128.0),
    )
    assert res["applied_count"] == 0
    assert res["errors"] and "write failed" in res["errors"][0]
    conn = sqlite3.connect(dedup_db)
    try:
        count = conn.execute(
            "SELECT count(*) FROM tag_provenance WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


@pytest.mark.requirement("META-01")
def test_an_in_memory_dedup_db_is_refused_before_any_write(tmp_path: Path) -> None:
    # Codex, PR #6182: provenance went to a second, empty in-memory database after the audio
    # write. Refused up front now: no backup taken, the file untouched.
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)
    with pytest.raises(ValueError, match="in-memory"):
        tags_apply.run_apply(
            files=[dst],
            dedup_db_path=Path(":memory:"),
            backup_root=tmp_path / "backups",
            reversal_dir=tmp_path / "reversals",
            allow_app_running=True,
            dry_run=False,
            fetch_rb=lambda p: TagRead(genre="Techno", bpm=128.0),
        )
    assert _sha(dst) == _sha(src)
    assert not (tmp_path / "backups").exists()


@pytest.mark.requirement("META-01")
def test_reversal_script_restores_original(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)
    before = _sha(dst)

    res = tags_apply.run_apply(
        files=[dst],
        dedup_db_path=tmp_path / "phase7.sqlite",
        backup_root=tmp_path / "backups",
        reversal_dir=tmp_path / "reversals",
        allow_app_running=True,
        dry_run=False,
        fetch_rb=lambda p: TagRead(title="Mutated", artist="Other"),
    )
    assert res["applied_count"] == 0
    assert res["reversal"] is None
    assert _sha(dst) == before


@pytest.mark.requirement("META-01")
def test_abort_if_rb_running(tmp_path: Path, monkeypatch) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "x.mp3"
    shutil.copy2(src, dst)
    before = _sha(dst)

    monkeypatch.setattr(
        tags_apply,
        "_is_app_running",
        lambda name: name == "rekordbox",
    )
    res = tags_apply.run_apply(
        files=[dst],
        dedup_db_path=tmp_path / "phase7.sqlite",
        backup_root=tmp_path / "backups",
        reversal_dir=tmp_path / "reversals",
        allow_app_running=False,
        dry_run=False,
        fetch_rb=lambda p: TagRead(title="X"),
    )
    assert res["applied_count"] == 0
    assert any("rekordbox running" in e for e in res["errors"])
    assert _sha(dst) == before


@pytest.mark.requirement("META-01")
def test_icloud_placeholder_skip(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "cloud.mp3"
    shutil.copy2(src, dst)
    # Create the .icloud sibling placeholder marker.
    sib = dst.with_name("." + dst.name + ".icloud")
    sib.write_text("placeholder")

    res = tags_apply.run_apply(
        files=[dst],
        dedup_db_path=tmp_path / "phase7.sqlite",
        backup_root=tmp_path / "backups",
        reversal_dir=tmp_path / "reversals",
        allow_app_running=True,
        dry_run=False,
        fetch_rb=lambda p: TagRead(title="X"),
    )
    assert res["applied_count"] == 0
    assert any("iCloud" in e for e in res["errors"])


@pytest.mark.requirement("META-01")
def test_cli_live_requires_i_understand(
    tmp_path: Path, capsys
) -> None:
    dst = tmp_path / "missing.mp3"
    rc = tags_apply.main([str(dst), "--live"])
    assert rc == 2
    assert "i-understand-the-risks" in capsys.readouterr().err


@pytest.mark.requirement("META-03")
def test_allow_app_running_keeps_backup_rail(tmp_path: Path) -> None:
    """`allow_app_running` must only disable the process-check rail.

    The backup rail (copy-before-write under ``backup_root``) must still
    fire on every live write. Regression for codex finding P07-01.
    """
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)

    backup_root = tmp_path / "backups"
    res = tags_apply.apply_one(
        dst,
        backup_root=backup_root,
        allow_app_running=True,
        dry_run=False,
        fetch_rb=lambda p: TagRead(title="Unified", artist="UArt"),
    )
    assert res.error and "write failed" in res.error
    assert res.backup is not None
    assert _sha(dst) == hashlib.sha256(src.read_bytes()).hexdigest()
    assert res.backup.exists()
    assert list(backup_root.rglob("*.mp3"))


@pytest.mark.requirement("META-03")
def test_allow_app_running_rejected_outside_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`allow_app_running=True` must raise when not running under pytest.

    It is a test-only escape hatch; production callers must not bypass
    the running-app safety check. Regression for codex finding P07-01.
    """
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "x.mp3"
    shutil.copy2(src, dst)

    monkeypatch.setattr(tags_apply, "_in_pytest", lambda: False)

    with pytest.raises(RuntimeError, match="test-only"):
        tags_apply.apply_one(
            dst,
            backup_root=tmp_path / "backups",
            allow_app_running=True,
            dry_run=True,
        )


def _patch_successful_tag_io(monkeypatch: pytest.MonkeyPatch, track: Path) -> None:
    written: dict = {}

    def fake_write_tags(path, unified, *, dry_run=True):
        written["tags"] = unified
        return WriteResult(path=path, applied={"genre": unified.genre}, dry_run=dry_run)

    def fake_read_tags(path):
        t = written["tags"]
        return TagRead(genre=t.genre, bpm=t.bpm)

    monkeypatch.setattr(tags_apply, "write_tags", fake_write_tags)
    monkeypatch.setattr(tags_apply, "read_tags", fake_read_tags)
    track.write_bytes(b"not-really-audio")


def _dedup_counts(dedup_db: Path) -> tuple[int, int]:
    conn = sqlite3.connect(dedup_db)
    try:
        prov = conn.execute("SELECT COUNT(*) FROM tag_provenance").fetchone()[0]
        ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return prov, ev
    finally:
        conn.close()


@pytest.mark.requirement("STATE-12741")
def test_dry_run_leaves_dedup_provenance_and_events_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] dry_run is true [then] provenance and events stay empty, [else stop]."""
    track = tmp_path / "Artist - Title.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", track)
    dedup_db = tmp_path / "phase7.sqlite"
    tags_apply.run_apply(
        files=[track],
        stable_id_lookup={str(track): "stable-dry"},
        dedup_db_path=dedup_db,
        backup_root=tmp_path / "backups",
        reversal_dir=tmp_path / "reversals",
        allow_app_running=True,
        dry_run=True,
        fetch_rb=lambda _p: TagRead(genre="Techno", bpm=128.0),
    )
    prov, ev = _dedup_counts(dedup_db)
    assert (prov, ev) == (0, 0), "if dry_run wrote provenance or events then broken"


@pytest.mark.requirement("STATE-12741")
def test_apply_one_provenance_appends_matching_events_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] provenance rows land in tag_provenance [then] a matching events row exists, [else stop]."""
    dedup_path = tmp_path / "phase7.sqlite"
    dedup_contract.ensure_schema(dedup_path)
    conn = sqlite3.connect(dedup_path, isolation_level=None)
    writer = StateWriter(conn, FakeEventBus(), actor="tags.apply")
    track = tmp_path / "track.mp3"
    _patch_successful_tag_io(monkeypatch, track)
    res = tags_apply.apply_one(
        track,
        backup_root=tmp_path / "backups",
        provenance_writer=writer,
        stable_id="stable-x",
        allow_app_running=True,
        fetch_rb=lambda _p: TagRead(genre="Techno", bpm=128.0),
        fetch_djay=lambda _p: None,
        fetch_mik=lambda _p: None,
    )
    writer.close()
    conn.close()
    assert res.error is None
    conn = sqlite3.connect(dedup_path)
    try:
        row_fields = {r[0] for r in conn.execute("SELECT field FROM tag_provenance")}
        events = conn.execute(
            "SELECT payload_json FROM events WHERE kind = 'tags.provenance'"
        ).fetchall()
        assert len(events) == 1, "if apply_one wrote provenance without an event then broken"
        payload_fields = {f["field"] for f in json.loads(events[0][0])["fields"]}
        assert payload_fields == row_fields and "genre" in row_fields
    finally:
        conn.close()


@pytest.mark.requirement("STATE-12741")
def test_apply_one_routes_provenance_through_state_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] apply_one writes tag_provenance [then] StateWriter.record_tag_provenance runs, [else stop]."""
    calls: list[str] = []
    dedup_path = tmp_path / "phase7.sqlite"
    dedup_contract.ensure_schema(dedup_path)
    conn = sqlite3.connect(dedup_path, isolation_level=None)
    writer = StateWriter(conn, FakeEventBus(), actor="tags.apply")
    original = StateWriter.record_tag_provenance

    def wrapped(self, **kwargs):
        calls.append("record_tag_provenance")
        return original(self, **kwargs)

    monkeypatch.setattr(StateWriter, "record_tag_provenance", wrapped)
    track = tmp_path / "track.mp3"
    _patch_successful_tag_io(monkeypatch, track)
    res = tags_apply.apply_one(
        track,
        backup_root=tmp_path / "backups",
        provenance_writer=writer,
        stable_id="stable-y",
        allow_app_running=True,
        fetch_rb=lambda _p: TagRead(genre="House"),
        fetch_djay=lambda _p: None,
        fetch_mik=lambda _p: None,
    )
    writer.close()
    conn.close()
    assert res.error is None
    assert calls == ["record_tag_provenance"], (
        "if apply_one used raw executemany plus commit for provenance then broken"
    )


@pytest.mark.requirement("STATE-12741")
def test_apply_module_has_no_bare_provenance_commit() -> None:
    """[if] tests cover apply_one [then] apply.py cannot commit provenance off the log, [else stop]."""
    tree = ast.parse(Path(tags_apply.__file__).read_text(encoding="utf-8"))
    attrs = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert not attrs & {"commit", "executemany"}, (
        "if apply.py writes provenance outside StateWriter then broken"
    )
