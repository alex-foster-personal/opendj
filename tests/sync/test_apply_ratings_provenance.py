"""SYNC-12986: a ratings apply run records per-decision write provenance.

[if] a live ratings run completes [then] every diff row has a provenance record, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.sync.apply_ratings import _row_provenance, live_run
from apps.sync.diff_csv import match_provenance

# Live-write mechanics against tmp fixtures only (never a real rb target).
pytestmark = [pytest.mark.requirement("SYNC-12986"), pytest.mark.rekordbox_writeback]


class _Content:
    """Duck-typed stand-in for a pyrekordbox ``DjmdContent`` row."""

    def __init__(self, rating: int = 0) -> None:
        self.Rating = rating


class _Query:
    """Duck-typed ``.one()`` wrapper over a single content row."""

    def __init__(self, value: _Content | None) -> None:
        self._value = value

    def one(self) -> _Content:
        """Return the row or raise like pyrekordbox does when absent."""
        if self._value is None:
            raise LookupError("not found")
        return self._value


class _RBDB:
    """Minimal ``open_db``-compatible stub that stores ratings in memory."""

    def __init__(self, contents: dict[str, _Content]) -> None:
        self._contents = contents

    def get_content(self, *, ID: str) -> _Query:
        """Look up one content row by id."""
        return _Query(self._contents.get(str(ID)))

    def commit(self) -> None:
        """Accept a commit (no-op)."""

    def close(self) -> None:
        """Accept a close (no-op)."""


def _rows() -> list[dict[str, str]]:
    """Return one ISRC write, one lower-confidence write and one conflict row."""
    return [
        {
            "rb_content_id": "10",
            "djay_uuid": "u-10",
            "field": "rating",
            "rb_value": "0",
            "djay_value": "5",
            "resolution": "accept_djay",
            "action_hint": "write djay -> RB",
            "match_confidence": "0.95",
            "match_signals": "isrc_exact|duration|title_artist",
        },
        {
            "rb_content_id": "20",
            "djay_uuid": "u-20",
            "field": "rating",
            "rb_value": "4",
            "djay_value": "0",
            "resolution": "accept_rb",
            "action_hint": "write RB -> djay",
            "match_confidence": "0.72",
            "match_signals": "duration|title_artist|bpm",
        },
        {
            "rb_content_id": "30",
            "djay_uuid": "u-30",
            "field": "rating",
            "rb_value": "3",
            "djay_value": "5",
            "resolution": "conflict",
            "action_hint": "manual review",
            "match_confidence": "0.90",
            "match_signals": "isrc_exact|duration|title_artist",
        },
    ]


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefer: str = "rb") -> dict[str, Any]:
    """Run live_run over the fixture rows under ``prefer`` and return the parsed artifact."""
    rb_db = tmp_path / "master.db"
    rb_db.write_bytes(b"rb")
    djay_db = tmp_path / "MediaLibrary.db"
    djay_db.write_bytes(b"djay")
    monkeypatch.setattr("apps.sync.safety._is_running", lambda _name: False)
    monkeypatch.setattr("apps.sync.safety.assert_icloud_quiesced", lambda *_a, **_k: None)
    monkeypatch.setattr("apps.shared.rekordbox_db.open_db", lambda _p: _RBDB({"10": _Content()}))
    # The djay writer reads the prior rating first (SYNC-12981 reversible record).
    prior_djay = {row["djay_uuid"]: int(row["djay_value"]) for row in _rows()}
    monkeypatch.setattr("apps.sync.apply_ratings._read_djay_rating", lambda _p, u: prior_djay[u])
    monkeypatch.setattr("apps.sync.apply_ratings._write_djay_rating", lambda _p, _u, _r: True)
    monkeypatch.setattr("apps.sync.apply_ratings._verify_djay_rating", lambda _p, _u, _r: True)
    out = tmp_path / "ratings-apply-result.json"
    assert (
        live_run(
            _rows(),
            flag_ok=True,
            rb_db_path=rb_db,
            djay_db_path=djay_db,
            prefer=prefer,
            provenance_out=out,
        )
        == 0
    )
    return json.loads(out.read_text(encoding="utf-8"))


def test_isrc_exact_write_records_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An exact-ISRC write records policy, confidence and planned ratings."""
    artifact = _run(tmp_path, monkeypatch)
    assert artifact["kind"] == "ratings-apply-result"
    assert artifact["prefer"] == "rb"
    rec = artifact["decisions"][0]
    assert rec["decision"] == "write"
    assert rec["resolution"] == "accept_djay"
    assert rec["match_method"] == "isrc_exact"
    assert rec["match_confidence"] == pytest.approx(0.95)
    assert (rec["source"], rec["target"]) == ("djay", "rekordbox")
    assert rec["source_rating"] == 5
    assert rec["target_rating_before"] == 0
    assert rec["planned_target_rating"] == 5
    assert rec["written"] is True


def test_lower_confidence_write_records_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A permitted lower-confidence write is labelled as such, not as ISRC."""
    rec = _run(tmp_path, monkeypatch)["decisions"][1]
    assert rec["decision"] == "write"
    assert rec["match_method"] == "lower_confidence"
    assert rec["match_confidence"] == pytest.approx(0.72)
    assert rec["match_signals"] == ["duration", "title_artist", "bpm"]
    assert (rec["source"], rec["target"]) == ("rekordbox", "djay")
    assert rec["planned_target_rating"] == 4
    assert rec["written"] is True


def test_conflict_row_records_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A conflict row no policy settles is recorded as refused with both side ratings and no plan."""
    artifact = _run(tmp_path, monkeypatch, prefer="newest")
    assert artifact["prefer"] == "newest"
    rec = artifact["decisions"][2]
    assert rec["decision"] == "refused_conflict"
    assert rec["rb_rating"] == 3
    assert rec["djay_rating"] == 5
    assert rec["planned_target_rating"] is None
    assert "written" not in rec
    assert artifact["written"] == {"rekordbox": 1, "djay": 1}


def test_conflict_row_settled_by_prefer_records_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A conflict row ``--prefer rb`` settles (SYNC-12980) is recorded as the write it was."""
    artifact = _run(tmp_path, monkeypatch, prefer="rb")
    rec = artifact["decisions"][2]
    assert rec["decision"] == "write"
    assert rec["resolution"] == "conflict"
    assert (rec["source"], rec["target"]) == ("rekordbox", "djay")
    assert rec["source_rating"] == 3
    assert rec["target_rating_before"] == 5
    assert rec["planned_target_rating"] == 3
    assert rec["written"] is True
    assert artifact["written"] == {"rekordbox": 1, "djay": 2}


def test_non_rating_row_is_recorded_as_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A diff row for another field (SYNC-12989) is never planned, so it is recorded as a skip."""
    row = {**_rows()[0], "field": "comment"}
    rec = _row_provenance(row, None, "rb")
    assert rec["decision"] == "skip"
    assert rec["planned_target_rating"] is None


def test_match_provenance_without_columns_is_unknown() -> None:
    """A legacy diff row without match columns reports an unknown method."""
    assert match_provenance({"resolution": "accept_rb"}) == {
        "match_confidence": None,
        "match_signals": [],
        "match_method": "unknown",
    }
