"""[if] live reconcile apply starts over a broken library [then] it aborts before backup, [else stop]."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.reconcile import apply
from apps.shared import library_integrity

pytestmark = pytest.mark.requirement("RECON-8946")


def _write_located_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "id",
        "title",
        "artist",
        "original_path",
        "best_candidate_path",
        "confidence",
        "signals_hit",
        "signal_count",
        "rationale",
        "triple_validated",
    ]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})


@dataclass(slots=True)
class _Track:
    folder_path: str
    is_streaming: bool = False


def _report(tmp_path: Path, *, present: int, broken: int) -> library_integrity.IntegrityReport:
    tracks: list[_Track] = []
    for i in range(present):
        f = tmp_path / f"present-{i}.aiff"
        f.write_bytes(b"\0")
        tracks.append(_Track(str(f)))
    tracks.extend(_Track(str(tmp_path / "gone" / f"missing-{i}.aiff")) for i in range(broken))
    return library_integrity.check_integrity(tracks)


def _minimal_located_csv(path: Path, track_id: str = "1") -> None:
    _write_located_csv(
        path,
        [
            {
                "id": track_id,
                "title": "t",
                "artist": "a",
                "original_path": "/old/path.aiff",
                "best_candidate_path": str(path.parent / "new.aiff"),
                "confidence": "1.0",
                "triple_validated": "True",
            }
        ],
    )
    (path.parent / "new.aiff").write_bytes(b"\0")


def test_gate_refuses_and_names_the_broken_ratio(tmp_path: Path) -> None:
    rep = _report(tmp_path, present=1, broken=3)
    with pytest.raises(apply.ApplyError) as exc:
        apply._integrity_gate(rep, allow_broken=False)
    assert "75.0%" in str(exc.value)


def test_gate_is_a_silent_no_op_on_a_healthy_library(tmp_path: Path) -> None:
    rep = _report(tmp_path, present=40, broken=0)
    assert apply._integrity_gate(rep, allow_broken=False) is None


def test_gate_returns_an_override_note_when_allow_broken(tmp_path: Path) -> None:
    rep = _report(tmp_path, present=1, broken=3)
    note = apply._integrity_gate(rep, allow_broken=True)
    assert note is not None and "75.0%" in note


def test_bad_integrity_threshold_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    csv_path = tmp_path / "located.csv"
    _minimal_located_csv(csv_path)
    monkeypatch.setattr(
        apply,
        "_live_integrity_report",
        lambda: _report(tmp_path, present=40, broken=0),
    )
    rc = apply.main(
        [
            "--live",
            "--i-understand-the-risks",
            "--tracks",
            "1",
            "--input",
            str(csv_path),
            "--backup-dir",
            str(tmp_path / "bk"),
            "--integrity-threshold",
            "10",
        ]
    )
    assert rc == 2


def test_live_tracks_aborts_before_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    csv_path = tmp_path / "located.csv"
    _minimal_located_csv(csv_path)
    monkeypatch.setattr(apply, "_live_integrity_report", lambda: _report(tmp_path, present=1, broken=3))

    def _no_backup(*args: object, **kwargs: object) -> Path:
        raise AssertionError("backup taken despite a failing integrity gate")

    monkeypatch.setattr(apply, "_backup_db", _no_backup)
    monkeypatch.setattr(apply, "_run_live", _no_backup)
    rc = apply.main(
        [
            "--live",
            "--i-understand-the-risks",
            "--tracks",
            "1",
            "--input",
            str(csv_path),
            "--backup-dir",
            str(tmp_path / "bk"),
        ]
    )
    assert rc == 9


def test_live_bulk_aborts_before_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    csv_path = tmp_path / "located.csv"
    _minimal_located_csv(csv_path)
    monkeypatch.setattr(apply, "_live_integrity_report", lambda: _report(tmp_path, present=1, broken=3))

    def _no_bulk(*args: object, **kwargs: object) -> int:
        raise AssertionError("_run_bulk entered despite a failing integrity gate")

    monkeypatch.setattr(apply, "_run_bulk", _no_bulk)
    rc = apply.main(
        [
            "--live",
            "--bulk",
            "--i-understand-the-risks",
            "--input",
            str(csv_path),
            "--backup-dir",
            str(tmp_path / "bk"),
        ]
    )
    assert rc == 9


def test_dry_run_does_not_call_integrity_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    csv_path = tmp_path / "located.csv"
    _minimal_located_csv(csv_path)

    def _boom() -> library_integrity.IntegrityReport:
        raise AssertionError("_live_integrity_report must not run on dry-run")

    monkeypatch.setattr(apply, "_live_integrity_report", _boom)
    rc = apply.main(["--dry-run", "--input", str(csv_path), "--patch-out", str(tmp_path / "p.json")])
    assert rc == 0


def test_allow_broken_reaches_run_live_with_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    csv_path = tmp_path / "located.csv"
    _minimal_located_csv(csv_path)
    monkeypatch.setattr(apply, "_live_integrity_report", lambda: _report(tmp_path, present=1, broken=3))
    captured: dict[str, object] = {}

    def _capture_run_live(*args: object, **kwargs: object) -> int:
        captured["integrity_override"] = kwargs.get("integrity_override")
        return 0

    monkeypatch.setattr(apply, "_run_live", _capture_run_live)
    rc = apply.main(
        [
            "--live",
            "--i-understand-the-risks",
            "--tracks",
            "1",
            "--allow-broken",
            "--input",
            str(csv_path),
            "--backup-dir",
            str(tmp_path / "bk"),
        ]
    )
    assert rc == 0
    note = captured.get("integrity_override")
    assert isinstance(note, str)
    assert "--allow-broken override" in note
    assert "75.0%" in note


def test_healthy_library_passes_none_override_to_run_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    csv_path = tmp_path / "located.csv"
    _minimal_located_csv(csv_path)
    monkeypatch.setattr(apply, "_live_integrity_report", lambda: _report(tmp_path, present=40, broken=0))
    captured: dict[str, object] = {}

    def _capture_run_live(*args: object, **kwargs: object) -> int:
        captured["integrity_override"] = kwargs.get("integrity_override")
        return 0

    monkeypatch.setattr(apply, "_run_live", _capture_run_live)
    rc = apply.main(
        [
            "--live",
            "--i-understand-the-risks",
            "--tracks",
            "1",
            "--input",
            str(csv_path),
            "--backup-dir",
            str(tmp_path / "bk"),
        ]
    )
    assert rc == 0
    assert captured.get("integrity_override") is None


def test_reversal_script_records_override_and_compiles(tmp_path: Path) -> None:
    note = apply._integrity_gate(_report(tmp_path, present=1, broken=3), allow_broken=True)
    assert note is not None
    backup = tmp_path / "master.db"
    backup.write_bytes(b"db")
    bk_dir = tmp_path / "bk"
    bk_dir.mkdir()
    updates = [
        apply.Update(
            id="1",
            title="t",
            artist="a",
            old_path="/old",
            new_path="/new",
            confidence=1.0,
            rationale="r",
            triple_validated=True,
        )
    ]
    rev = apply._write_reversal_script(updates, backup, bk_dir, "ts", integrity_override=note)
    text = rev.read_text(encoding="utf-8")
    assert "--allow-broken override" in text
    assert "75.0%" in text
    compile(text, rev.name, "exec")

    bk2 = tmp_path / "bk2"
    bk2.mkdir()
    rev_ok = apply._write_reversal_script(updates, backup, bk2, "ts2", integrity_override=None)
    text_ok = rev_ok.read_text(encoding="utf-8")
    assert "no override was used" in text_ok
    assert "--allow-broken override" not in text_ok
    compile(text_ok, rev_ok.name, "exec")
