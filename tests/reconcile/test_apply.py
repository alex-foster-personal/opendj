"""Tests for :mod:`apps.reconcile.apply`. Ties to RECON-03, RECON-04.

We never touch the live Rekordbox DB — any test that would exercise the
write path uses the ``tmp_rb_db`` fixture (copy-per-test). CLI safety
rails (``--live`` without ``--i-understand-the-risks``, running-RB check,
bulk mode flag combinations) are exercised via ``main(argv=...)``.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from apps.reconcile import apply


# ------------------------------------------------------------------ helpers


def _write_located_csv(path: Path, rows: list[dict[str, object]]) -> None:
    """Write a minimal located.csv for apply._load_updates to consume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "id", "title", "artist", "original_path",
        "best_candidate_path", "confidence", "signals_hit",
        "signal_count", "rationale", "triple_validated",
    ]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})


# ------------------------------------------------------------------ loading


@pytest.mark.requirement("RECON-03")
def test_load_updates_skips_rows_without_candidate(tmp_path: Path) -> None:
    """Rows with empty ``best_candidate_path`` are silently skipped."""
    csv_path = tmp_path / "located.csv"
    _write_located_csv(
        csv_path,
        [
            {"id": "1", "best_candidate_path": "/a/x.mp3", "confidence": 0.9,
             "triple_validated": "True", "original_path": "/old/x.mp3"},
            {"id": "2", "best_candidate_path": "", "confidence": 0.0,
             "triple_validated": "False"},
        ],
    )
    updates = apply._load_updates(csv_path)
    assert [u.id for u in updates] == ["1"]


@pytest.mark.requirement("RECON-03")
def test_load_updates_raises_systemexit_if_csv_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        apply._load_updates(tmp_path / "nope.csv")


# ------------------------------------------------------------------ filtering


@pytest.mark.requirement("RECON-03")
def test_filter_updates_defaults_to_triple_validated_only() -> None:
    """Without ``--tracks``, only triple-validated updates pass through."""
    u1 = apply.Update(id="1", title="", artist="", old_path="", new_path="/a", confidence=0.9, rationale="", triple_validated=True)
    u2 = apply.Update(id="2", title="", artist="", old_path="", new_path="/b", confidence=0.2, rationale="", triple_validated=False)
    out = apply._filter_updates([u1, u2], track_ids=None)
    assert [u.id for u in out] == ["1"]


@pytest.mark.requirement("RECON-03")
def test_filter_updates_with_track_ids_keeps_order_and_includes_unvalidated() -> None:
    """``--tracks 2,1`` keeps user-specified order + emits even non-validated."""
    u1 = apply.Update(id="1", title="", artist="", old_path="", new_path="/a", confidence=0.9, rationale="", triple_validated=True)
    u2 = apply.Update(id="2", title="", artist="", old_path="", new_path="/b", confidence=0.2, rationale="", triple_validated=False)
    out = apply._filter_updates([u1, u2], track_ids=["2", "1"])
    assert [u.id for u in out] == ["2", "1"]


# ------------------------------------------------------------------ dry-run


@pytest.mark.requirement("RECON-03")
def test_dry_run_writes_patch_json(tmp_path: Path) -> None:
    """Dry-run emits a parseable ``patch.json`` with the expected shape."""
    csv_path = tmp_path / "located.csv"
    _write_located_csv(
        csv_path,
        [
            {"id": "1", "best_candidate_path": "/new/x.mp3", "confidence": 0.9,
             "triple_validated": "True", "original_path": "/old/x.mp3",
             "rationale": "basename match", "title": "T", "artist": "A"},
        ],
    )
    patch_out = tmp_path / "patch.json"
    rc = apply.main([
        "--dry-run",
        "--input", str(csv_path),
        "--patch-out", str(patch_out),
    ])
    assert rc == 0
    data = json.loads(patch_out.read_text())
    assert data == [
        {
            "id": "1",
            "old_path": "/old/x.mp3",
            "new_path": "/new/x.mp3",
            "confidence": 0.9,
            "rationale": "basename match",
        }
    ]


# ------------------------------------------------------------------ CLI rails


@pytest.mark.requirement("RECON-04")
def test_live_requires_i_understand_flag(tmp_path: Path) -> None:
    """``--live`` without ``--i-understand-the-risks`` aborts with exit 2."""
    csv_path = tmp_path / "located.csv"
    _write_located_csv(csv_path, [
        {"id": "1", "best_candidate_path": "/x", "confidence": 0.9,
         "triple_validated": "True", "original_path": "/old"},
    ])
    rc = apply.main([
        "--live", "--tracks", "1",
        "--input", str(csv_path),
    ])
    assert rc == 2


@pytest.mark.requirement("RECON-04")
def test_live_requires_tracks_when_not_bulk(tmp_path: Path) -> None:
    """``--live`` without ``--tracks`` AND without ``--bulk`` → exit 2."""
    csv_path = tmp_path / "located.csv"
    _write_located_csv(csv_path, [])
    rc = apply.main([
        "--live", "--i-understand-the-risks",
        "--input", str(csv_path),
    ])
    assert rc == 2


@pytest.mark.requirement("RECON-04")
def test_live_enforces_max_live_tracks(tmp_path: Path) -> None:
    """More than ``MAX_LIVE_TRACKS`` IDs → exit 2 (must use --bulk instead)."""
    csv_path = tmp_path / "located.csv"
    _write_located_csv(csv_path, [])
    ids = ",".join(str(i) for i in range(apply.MAX_LIVE_TRACKS + 1))
    rc = apply.main([
        "--live", "--i-understand-the-risks",
        "--tracks", ids,
        "--input", str(csv_path),
    ])
    assert rc == 2


@pytest.mark.requirement("RECON-04")
def test_bulk_requires_live(tmp_path: Path) -> None:
    """``--bulk`` without ``--live`` → exit 2."""
    csv_path = tmp_path / "located.csv"
    _write_located_csv(csv_path, [])
    rc = apply.main([
        "--bulk", "--i-understand-the-risks",
        "--input", str(csv_path),
    ])
    assert rc == 2


@pytest.mark.requirement("RECON-04")
def test_bulk_requires_i_understand_flag(tmp_path: Path) -> None:
    """``--bulk --live`` without ``--i-understand-the-risks`` → exit 2."""
    csv_path = tmp_path / "located.csv"
    _write_located_csv(csv_path, [])
    rc = apply.main([
        "--bulk", "--live",
        "--input", str(csv_path),
    ])
    assert rc == 2


@pytest.mark.requirement("RECON-04")
def test_bulk_mutex_with_tracks(tmp_path: Path) -> None:
    """``--bulk`` + ``--tracks`` → exit 2 (mutually exclusive)."""
    csv_path = tmp_path / "located.csv"
    _write_located_csv(csv_path, [])
    rc = apply.main([
        "--bulk", "--live", "--i-understand-the-risks",
        "--tracks", "1",
        "--input", str(csv_path),
    ])
    assert rc == 2


@pytest.mark.requirement("RECON-04")
def test_bulk_mutex_with_dry_run(tmp_path: Path) -> None:
    """``--bulk`` + ``--dry-run`` → exit 2."""
    csv_path = tmp_path / "located.csv"
    _write_located_csv(csv_path, [])
    rc = apply.main([
        "--bulk", "--dry-run",
        "--input", str(csv_path),
    ])
    assert rc == 2


@pytest.mark.requirement("RECON-04")
def test_rekordbox_running_check_uses_pgrep(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_rekordbox_running`` returns True when pgrep matches, else False."""
    import subprocess

    class _FakeResult:
        def __init__(self, code: int, stdout: str) -> None:
            self.returncode = code
            self.stdout = stdout

    def _fake_run(args, **kwargs):  # noqa: ANN001
        return _FakeResult(0, "12345 Rekordbox\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert apply._rekordbox_running() is True

    def _fake_empty(args, **kwargs):  # noqa: ANN001
        return _FakeResult(1, "")

    monkeypatch.setattr(subprocess, "run", _fake_empty)
    assert apply._rekordbox_running() is False


# ------------------------------------------------------------------ write path


@pytest.mark.requirement("RECON-04")
def test_write_reversal_script_is_self_contained(tmp_path: Path) -> None:
    """The generated reverse-*.py script is syntactically valid Python."""
    u = apply.Update(
        id="42", title="t", artist="a",
        old_path="/old.mp3", new_path="/new.mp3",
        confidence=0.9, rationale="", triple_validated=True,
    )
    script_dir = tmp_path / "backups"
    script_dir.mkdir()
    backup = script_dir / "master.XYZ.db"
    backup.write_bytes(b"stub")
    out = apply._write_reversal_script([u], backup, script_dir, "XYZ")
    assert out.exists()
    assert out.suffix == ".py"
    # Must compile cleanly — catches any f-string/escaping bugs.
    compile(out.read_text(), str(out), "exec")


@pytest.mark.requirement("RECON-04")
def test_tmp_rb_db_fixture_is_a_copy(tmp_rb_db: Path, rb_plain_db_path: Path) -> None:
    """Sanity check the tests/conftest contract: tmp_rb_db is never the shared
    fixture path. Mutating it must not affect the committed DB."""
    assert tmp_rb_db != rb_plain_db_path
    # File paths are different; contents begin identical.
    assert tmp_rb_db.read_bytes()[:16] == rb_plain_db_path.read_bytes()[:16]


# ------------------------------------------------------------------ internals


@pytest.mark.requirement("RECON-03")
def test_parse_ids_handles_whitespace_and_empty() -> None:
    assert apply._parse_ids(None) is None
    assert apply._parse_ids("") is None
    assert apply._parse_ids("  1, 2 ,3,,") == ["1", "2", "3"]
    assert apply._parse_ids("42") == ["42"]


@pytest.mark.requirement("RECON-03")
def test_write_patch_serializes_rationale_and_confidence(tmp_path: Path) -> None:
    u = apply.Update(
        id="x", title="t", artist="a",
        old_path="/old", new_path="/new",
        confidence=0.75, rationale="r", triple_validated=True,
    )
    out = tmp_path / "patch.json"
    apply._write_patch([u], out)
    data = json.loads(out.read_text())
    assert data[0]["confidence"] == 0.75
    assert data[0]["rationale"] == "r"


@pytest.mark.requirement("RECON-03")
def test_main_no_mode_flag_prints_help_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Running with no flags prints usage and exits 0 (help-style)."""
    csv_path = tmp_path / "located.csv"
    _write_located_csv(csv_path, [])
    rc = apply.main(["--input", str(csv_path)])
    assert rc == 0
    assert "apps.reconcile.apply" in capsys.readouterr().out


@pytest.mark.requirement("RECON-03")
def test_run_dry_run_empty_updates_still_writes_patch(tmp_path: Path) -> None:
    """Even with zero updates, ``_run_dry_run`` emits an empty patch.json."""
    out = tmp_path / "patch.json"
    rc = apply._run_dry_run([], out)
    assert rc == 0
    assert json.loads(out.read_text()) == []


@pytest.mark.requirement("RECON-04")
def test_preflight_fs_check_flags_missing_new_paths(tmp_path: Path) -> None:
    """``_preflight_fs_check`` returns updates whose new_path does NOT exist."""
    real = tmp_path / "exists.mp3"
    real.write_bytes(b"stub")
    u_ok = apply.Update(
        id="1", title="", artist="", old_path="", new_path=str(real),
        confidence=1.0, rationale="", triple_validated=True,
    )
    u_bad = apply.Update(
        id="2", title="", artist="", old_path="", new_path=str(tmp_path / "nope.mp3"),
        confidence=1.0, rationale="", triple_validated=True,
    )
    missing = apply._preflight_fs_check([u_ok, u_bad])
    assert [u.id for u in missing] == ["2"]


@pytest.mark.requirement("RECON-04")
def test_select_bulk_updates_intersects_triple_validated_and_broken() -> None:
    """Bulk selection = triple_validated ∩ still-broken ∩ has new_path."""
    updates = [
        apply.Update(id="A", title="", artist="", old_path="", new_path="/a",
                     confidence=1.0, rationale="", triple_validated=True),
        apply.Update(id="B", title="", artist="", old_path="", new_path="/b",
                     confidence=0.5, rationale="", triple_validated=False),
        apply.Update(id="C", title="", artist="", old_path="", new_path="/c",
                     confidence=0.9, rationale="", triple_validated=True),
        apply.Update(id="D", title="", artist="", old_path="", new_path="",
                     confidence=0.9, rationale="", triple_validated=True),
    ]
    out = apply._select_bulk_updates(updates, broken_ids={"A", "B", "D"})
    assert [u.id for u in out] == ["A"]  # C excluded (not broken), B/D excluded


@pytest.mark.requirement("RECON-04")
def test_load_broken_ids_returns_ids_only(tmp_path: Path) -> None:
    """Bulk broken-CSV loader pulls the ``id`` column into a set."""
    csv_path = tmp_path / "broken.csv"
    csv_path.write_text("id,title,artist\n1,a,b\n2,c,d\n,empty,row\n")
    ids = apply._load_broken_ids(csv_path)
    assert ids == {"1", "2"}


@pytest.mark.requirement("RECON-04")
def test_load_broken_ids_exits_when_csv_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        apply._load_broken_ids(tmp_path / "nope.csv")


# ------------------------------------------------------------------ live aborts


@pytest.mark.requirement("RECON-04")
def test_run_live_aborts_when_no_updates() -> None:
    """``_run_live([])`` returns 1 without any prompts or writes."""
    assert apply._run_live([], Path("/tmp/unused-backup-dir")) == 1


@pytest.mark.requirement("RECON-04")
def test_run_live_refuses_more_than_max_tracks(tmp_path: Path) -> None:
    """More updates than ``MAX_LIVE_TRACKS`` → exit 2 before any IO."""
    ups = [
        apply.Update(id=str(i), title="", artist="", old_path="",
                     new_path="/x", confidence=1.0, rationale="",
                     triple_validated=True)
        for i in range(apply.MAX_LIVE_TRACKS + 1)
    ]
    assert apply._run_live(ups, tmp_path) == 2


@pytest.mark.requirement("RECON-04")
def test_run_live_aborts_when_rekordbox_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If pgrep says RB is running, abort with exit 3 BEFORE writing anything."""
    monkeypatch.setattr(apply, "_rekordbox_running", lambda: True)
    u = apply.Update(id="1", title="", artist="", old_path="",
                     new_path="/x", confidence=1.0, rationale="",
                     triple_validated=True)
    assert apply._run_live([u], tmp_path) == 3


@pytest.mark.requirement("RECON-04")
def test_run_live_aborts_when_confirmation_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the user doesn't type the magic phrase, abort with exit 4."""
    monkeypatch.setattr(apply, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(apply, "_confirm", lambda: False)
    u = apply.Update(id="1", title="", artist="", old_path="",
                     new_path="/x", confidence=1.0, rationale="",
                     triple_validated=True)
    assert apply._run_live([u], tmp_path) == 4


@pytest.mark.requirement("RECON-04")
def test_run_bulk_aborts_when_no_updates_selected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_run_bulk`` with empty selection returns 1 and writes nothing."""
    # Stub broken_ids loader and selection so nothing survives the filter.
    monkeypatch.setattr(apply, "_load_broken_ids", lambda p: set())
    monkeypatch.setattr(apply, "_select_bulk_updates", lambda u, b: [])
    assert apply._run_bulk([], tmp_path / "broken.csv", tmp_path / "bk") == 1


@pytest.mark.requirement("RECON-04")
def test_run_bulk_aborts_when_rekordbox_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bulk mode: RB running → exit 3."""
    u = apply.Update(id="1", title="", artist="", old_path="",
                     new_path="/x", confidence=1.0, rationale="",
                     triple_validated=True)
    monkeypatch.setattr(apply, "_load_broken_ids", lambda p: {"1"})
    monkeypatch.setattr(apply, "_select_bulk_updates", lambda u, b: list(u))
    monkeypatch.setattr(apply, "_rekordbox_running", lambda: True)
    assert apply._run_bulk([u], tmp_path / "broken.csv", tmp_path / "bk") == 3


@pytest.mark.requirement("RECON-04")
def test_run_bulk_preflight_fs_missing_aborts_with_7(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pre-flight FS check flags missing new_path → exit 7 before backup."""
    u = apply.Update(id="1", title="", artist="", old_path="",
                     new_path=str(tmp_path / "never.mp3"), confidence=1.0,
                     rationale="", triple_validated=True)
    monkeypatch.setattr(apply, "_load_broken_ids", lambda p: {"1"})
    monkeypatch.setattr(apply, "_select_bulk_updates", lambda u, b: list(u))
    monkeypatch.setattr(apply, "_rekordbox_running", lambda: False)
    assert apply._run_bulk([u], tmp_path / "broken.csv", tmp_path / "bk") == 7


@pytest.mark.requirement("RECON-03")
def test_print_preview_does_not_raise_on_empty(capsys: pytest.CaptureFixture[str]) -> None:
    apply._print_preview([], banner="X")
    out = capsys.readouterr().out
    assert "0 pending update(s)" in out
