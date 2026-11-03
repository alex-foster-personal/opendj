"""CLI tests for set notes (SETNOTE-6340c), run as a real subprocess.

[if] python -m apps.set_notes runs on a data dir [then] list/get/put/409 match HTTP, [else stop].
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from apps.set_notes.store import Geometry, open_set_notes, save_set_note

from tests.webui.set_notes_test_db import init_empty_state_db

pytestmark = pytest.mark.requirement("SETNOTE-6340c")

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    db_path = tmp_path / "state" / "state.db"
    init_empty_state_db(db_path)
    return tmp_path


def _cli(data_dir: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "apps.set_notes", *args, "--data-dir", str(data_dir)],
        cwd=REPO,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def test_put_get_list_and_stale(data_dir: Path, tmp_path: Path) -> None:
    """[if] the CLI writes, reads and lists [then] revs advance and stale is exit 3, [else stop]."""
    assert json.loads(_cli(data_dir, "list").stdout) == []
    missing = _cli(data_dir, "get", "scratch")
    assert missing.returncode == 1, missing.stderr
    assert json.loads(missing.stderr) == {"error": "not_found", "scope": "scratch"}

    text = "# Set\n- [[track:T1|A - B]]"
    body = tmp_path / "note.md"
    body.write_text(text, encoding="utf-8")
    created = _cli(data_dir, "put", "scratch", "--body-file", str(body), "--base-rev", "0")
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["rev"] == 1

    got = json.loads(_cli(data_dir, "get", "scratch").stdout)
    assert (got["body_md"], got["rev"]) == (text, 1)

    stale = _cli(data_dir, "put", "scratch", "--body-file", "-", "--base-rev", "0", stdin="lost")
    assert stale.returncode == 3, stale.stdout
    assert json.loads(stale.stderr) == {"error": "stale_rev", "current_rev": 1}

    listed = json.loads(_cli(data_dir, "list").stdout)
    assert [(r["scope"], r["rev"], r["chars"]) for r in listed] == [("scratch", 1, len(text))]


def test_put_keeps_pad_geometry(data_dir: Path) -> None:
    """[if] the CLI rewrites a body the pad saved [then] geometry and visible stay, [else stop]."""
    conn = open_set_notes(data_dir / "state" / "state.db")
    save_set_note(conn, "playlist:p1", "old", Geometry(x=5, y=6, w=300, h=200), True, 0)
    conn.commit()
    conn.close()
    put = _cli(data_dir, "put", "playlist:p1", "--body-file", "-", "--base-rev", "1", stdin="new")
    assert put.returncode == 0, put.stderr
    note = json.loads(put.stdout)
    assert (note["body_md"], note["rev"], note["visible"]) == ("new", 2, True)
    assert (note["x"], note["y"], note["w"], note["h"]) == (5, 6, 300, 200)


def test_bad_scope_and_missing_db_exit_2(data_dir: Path, tmp_path: Path) -> None:
    """[if] the scope is invalid or state.db is absent [then] the CLI exits 2, [else stop]."""
    bad = _cli(data_dir, "get", "nope")
    assert bad.returncode == 2, bad.stderr
    assert json.loads(bad.stderr)["error"] == "invalid"
    absent = _cli(tmp_path / "no-data", "list")
    assert absent.returncode == 2, absent.stderr
    assert json.loads(absent.stderr)["error"] == "state_db_missing"
