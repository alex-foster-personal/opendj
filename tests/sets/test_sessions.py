"""Tests for :mod:`apps.sets.sessions`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.sets import sessions as sessions_mod
from apps.sets.manifest import Manifest, write_manifest


def _seed_session(
    sets_root: Path,
    session_id: str,
    *,
    ended_at: str | None = "2026-04-17T23:45:00.000+00:00",
    started_at: str = "2026-04-17T21:30:00.000+00:00",
    transitions: list[dict] | None = None,
    labels: list[tuple[int, str]] | None = None,
) -> Path:
    sess = sets_root / session_id
    sess.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        session_id=session_id,
        started_at=started_at,
        ended_at=ended_at,
        capture_device="BlackHole 2ch",
        event_count=3,
        deck_sources=["djay_monitor"],
        mp3_segments=[],
    )
    write_manifest(sess, manifest)
    if transitions:
        (sess / "transitions.jsonl").write_text(
            "\n".join(json.dumps(t) for t in transitions) + "\n"
        )
    if labels:
        (sess / "labels.jsonl").write_text(
            "\n".join(json.dumps({"idx": i, "class": c}) for i, c in labels) + "\n"
        )
    return sess


@pytest.mark.requirement("SET-03")
def test_list_sessions_returns_sorted_by_start_desc(sets_root: Path):
    _seed_session(sets_root, "s-early", started_at="2026-04-10T10:00:00+00:00")
    _seed_session(sets_root, "s-late", started_at="2026-04-17T21:30:00+00:00")
    summaries = sessions_mod.list_sessions(sets_root=sets_root)
    assert [s.session_id for s in summaries] == ["s-late", "s-early"]


@pytest.mark.requirement("SET-03")
def test_list_sessions_skips_dirs_without_manifest(sets_root: Path):
    (sets_root / "no-manifest").mkdir()
    _seed_session(sets_root, "with-manifest")
    summaries = sessions_mod.list_sessions(sets_root=sets_root)
    assert [s.session_id for s in summaries] == ["with-manifest"]


@pytest.mark.requirement("SET-03")
def test_list_sessions_counts_transitions(sets_root: Path):
    _seed_session(
        sets_root,
        "s-count",
        transitions=[{"idx": i} for i in range(5)],
    )
    summary = sessions_mod.list_sessions(sets_root=sets_root)[0]
    assert summary.transition_count == 5


@pytest.mark.requirement("SET-03")
def test_get_session_loads_transitions_and_labels(sets_root: Path):
    _seed_session(
        sets_root,
        "s-full",
        transitions=[{"idx": 0, "predicted_class": "cut"}],
        labels=[(0, "blend")],
    )
    session = sessions_mod.get_session("s-full", sets_root=sets_root)
    assert session is not None
    assert session.transitions[0]["predicted_class"] == "cut"
    assert session.labels == {0: "blend"}
    assert session.summary.share_state == "private"


@pytest.mark.requirement("SET-03")
def test_get_session_returns_none_for_missing(sets_root: Path):
    assert sessions_mod.get_session("nope", sets_root=sets_root) is None


@pytest.mark.requirement("SET-03")
def test_summary_duration_computed(sets_root: Path):
    _seed_session(
        sets_root,
        "s-dur",
        started_at="2026-04-17T21:30:00+00:00",
        ended_at="2026-04-17T22:15:00+00:00",
    )
    summary = sessions_mod.list_sessions(sets_root=sets_root)[0]
    assert summary.duration_s == 45 * 60
