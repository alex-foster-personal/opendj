"""Tests for :mod:`apps.open_dj.diff` -- structural diff of open-dj docs."""
from __future__ import annotations

import copy

import pytest

from apps.open_dj.diff import diff_documents, format_report


@pytest.mark.requirement("OPEN-03")
def test_identical_docs_yield_empty_diff(full_doc: dict) -> None:
    a = copy.deepcopy(full_doc)
    b = copy.deepcopy(full_doc)
    report = diff_documents(a, b)
    assert report.is_empty
    assert not report.added and not report.removed and not report.mutated
    assert format_report(report) == "no differences"


@pytest.mark.requirement("OPEN-03")
def test_added_track_reported(full_doc: dict) -> None:
    a = copy.deepcopy(full_doc)
    b = copy.deepcopy(full_doc)
    # Remove one track from `a` so `b` has one extra.
    removed_tid = a["tracks"].pop()["track_id"]
    report = diff_documents(a, b)
    assert report.added == [removed_tid]
    assert not report.removed
    assert not report.mutated


@pytest.mark.requirement("OPEN-03")
def test_removed_track_reported(full_doc: dict) -> None:
    a = copy.deepcopy(full_doc)
    b = copy.deepcopy(full_doc)
    removed_tid = b["tracks"].pop()["track_id"]
    report = diff_documents(a, b)
    assert report.removed == [removed_tid]
    assert not report.added


@pytest.mark.requirement("OPEN-03")
def test_mutated_identity_field_reported(full_doc: dict) -> None:
    a = copy.deepcopy(full_doc)
    b = copy.deepcopy(full_doc)
    b["tracks"][0]["title"] = "New Title"
    report = diff_documents(a, b)
    assert not report.is_empty
    assert len(report.mutated) == 1
    assert report.mutated[0].track_id == a["tracks"][0]["track_id"]
    changes = {ch.field_name: (ch.before, ch.after) for ch in report.mutated[0].changes}
    assert "title" in changes
    assert changes["title"] == ("Lanterns", "New Title")


@pytest.mark.requirement("OPEN-03")
def test_provenance_subfield_diff(full_doc: dict) -> None:
    """Changing ProvenanceValue.source emits a `bpm.source` FieldChange."""
    a = copy.deepcopy(full_doc)
    b = copy.deepcopy(full_doc)
    b["tracks"][0]["bpm"]["source"] = "open-dj-tool"
    report = diff_documents(a, b)
    assert report.mutated
    changes = {ch.field_name: ch for ch in report.mutated[0].changes}
    # Only `bpm.source` should change (modified_at, value stay equal).
    assert "bpm.source" in changes
    assert changes["bpm.source"].before == "rekordbox"
    assert changes["bpm.source"].after == "open-dj-tool"
    assert "bpm.value" not in changes
    assert "bpm.modified_at" not in changes


@pytest.mark.requirement("OPEN-03")
def test_format_report_has_human_summary(full_doc: dict) -> None:
    a = copy.deepcopy(full_doc)
    b = copy.deepcopy(full_doc)
    b["tracks"][0]["title"] = "NEW"
    text = format_report(diff_documents(a, b))
    assert "mutated" in text
    assert "NEW" in text


@pytest.mark.requirement("OPEN-03")
def test_diff_ignores_playlist_changes_at_v0_2(full_doc: dict) -> None:
    """v0.2 diff scope is tracks only (strawman section 10.3)."""
    a = copy.deepcopy(full_doc)
    b = copy.deepcopy(full_doc)
    b["playlists"][0]["name"] = "Different Name"
    report = diff_documents(a, b)
    assert report.is_empty
