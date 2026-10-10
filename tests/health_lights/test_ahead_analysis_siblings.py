"""NATIVE-24 (#5578): two rows for one file are analysed once by the ahead-of-time drain.

Regression lines:
  - if a row whose same-file sibling has the lane's result is enqueued then broken
  - if a row with a unique path is skipped without its own result then broken
  - if a covered sibling still counts as missing in coverage then broken

[if] the drain decodes one file twice for one lane [then] fail, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.webui.server import ahead_analysis as aa
from apps.webui.server.ahead_analysis_siblings import with_path_siblings

pytestmark = pytest.mark.requirement("NATIVE-24")

#: a and b are two rows for one file (b's path is the NFD spelling); c is its own file.
PATHS = {"a": "/m/Café.mp3", "b": "/m/Café.mp3", "c": "/m/other.mp3"}


def _drain(
    runs: list[tuple[str, list[str]]],
    *,
    with_paths: bool,
    hashes: dict[str, str] | None = None,
) -> aa.AheadDrain:
    done = {lane: {"a"} for lane, _b in aa.LANE_ORDER}

    def run_lane(lane: str, _backend: str, ids: list[str]) -> dict[str, str]:
        runs.append((lane, list(ids)))
        done[lane].update(ids)
        return {}

    return aa.AheadDrain(
        aa.AheadSources(
            present_fn=lambda: list(PATHS),
            mapped_fn=set,
            has_strip_fn=lambda _sid: True,
            write_strip_fn=lambda _sid: None,
            done_fn=lambda lane, _backend: set(done[lane]),
            run_lane_fn=run_lane,
            playing_fn=lambda: False,
            blank_tags_fn=set,
            refresh_tags_fn=lambda _sid: True,
            declined_fn=lambda _lane, _backend: {},
            paths_fn=(lambda: dict(PATHS)) if with_paths else None,
            hashes_fn=(lambda: dict(hashes)) if hashes is not None else None,
        )
    )


def _run_to_green(drain: aa.AheadDrain) -> None:
    for _ in range(50):
        if drain.tick() == "green":
            return
    raise AssertionError("drain never went green")


def test_a_covered_sibling_is_never_enqueued() -> None:
    """[if] a same-hash sibling has the lane's result [then] it is not enqueued, [else stop]."""
    same = "sha256:" + "ab" * 32
    runs: list[tuple[str, list[str]]] = []
    _run_to_green(_drain(runs, with_paths=True, hashes={"a": same, "b": same, "c": "sha256:" + "cc" * 32}))
    assert runs and all(ids == ["c"] for _lane, ids in runs), runs


def test_mutation_control_without_paths_the_sibling_is_decoded_again() -> None:
    """[if] the drain has no path map [then] the sibling is enqueued, proving the guard bites, [else stop]."""
    runs: list[tuple[str, list[str]]] = []
    _run_to_green(_drain(runs, with_paths=False))
    assert any("b" in ids for _lane, ids in runs), "if the sibling is skipped with no path map then the test is blind"


def test_coverage_counts_a_covered_sibling_done() -> None:
    """[if] a same-hash sibling is covered [then] coverage counts it done, [else stop]."""
    same = "sha256:" + "ab" * 32
    cov = _drain(
        [], with_paths=True, hashes={"a": same, "b": same, "c": "sha256:" + "cc" * 32}
    ).refresh_coverage()
    assert (cov["lanes"]["loudness"]["done"], cov["lanes"]["loudness"]["missing"]) == (2, 1)


def test_a_unique_path_needs_its_own_result() -> None:
    """[if] a done row shares no path [then] only it is done, [else stop]."""
    hashes = {"a": "sha256:" + "ab" * 32, "b": "sha256:" + "ab" * 32, "c": "sha256:" + "cc" * 32}
    assert with_path_siblings({"c"}, PATHS, hashes) == {"c"}
    assert with_path_siblings({"a"}, PATHS, hashes) == {"a", "b"}
    assert with_path_siblings(set(), PATHS, hashes) == set()
    assert with_path_siblings({"a"}, PATHS, {"a": "sha256:" + "ab" * 32, "b": "sha256:" + "cc" * 32}) == {"a"}


def test_a_drain_without_hashes_still_enqueues_the_sibling() -> None:
    """[if] the drain has paths but no hashes [then] the sibling is enqueued, [else stop]."""
    runs: list[tuple[str, list[str]]] = []
    _run_to_green(_drain(runs, with_paths=True))
    assert any("b" in ids for _lane, ids in runs), runs


def test_a_different_audio_hash_is_still_enqueued() -> None:
    """[if] audio hashes differ [then] the drain still enqueues that sibling, [else stop]."""
    hashes = {
        "a": "sha256:" + "aa" * 32,
        "b": "sha256:" + "bb" * 32,
        "c": "sha256:" + "cc" * 32,
    }
    runs: list[tuple[str, list[str]]] = []
    _run_to_green(_drain(runs, with_paths=True, hashes=hashes))
    assert any("b" in ids for _lane, ids in runs), runs


def test_a_matching_audio_hash_is_not_enqueued_twice() -> None:
    """[if] audio hashes match [then] the drain does not enqueue that sibling, [else stop]."""
    same = "sha256:" + "ab" * 32
    hashes = {"a": same, "b": same, "c": "sha256:" + "cc" * 32}
    runs: list[tuple[str, list[str]]] = []
    _run_to_green(_drain(runs, with_paths=True, hashes=hashes))
    assert runs and all("b" not in ids for _lane, ids in runs), runs


def _window_pair(tmp_path: Path):
    """Warm the present snapshot, and read hashes once, while the pair still matches."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, file_path TEXT, audio_hash TEXT, deleted_at TEXT)"
    )
    same = "sha256:" + "ab" * 32
    changed = "sha256:" + "ff" * 32
    path = "/m/track.mp3"
    conn.executemany(
        "INSERT INTO tracks (stable_id, file_path, audio_hash) VALUES (?, ?, ?)",
        [
            ("a", path, same),
            ("b", path, same),
            ("c", "/m/other.mp3", "sha256:" + "cc" * 32),
        ],
    )
    conn.commit()
    calls = {"n": 0}

    def snapshot() -> SimpleNamespace:
        calls["n"] += 1
        return SimpleNamespace(on_disk=[("a", path), ("b", path), ("c", "/m/other.mp3")])

    rows = aa.PlayableRows(
        snapshot_fn=snapshot,
        open_ro=lambda: sqlite3.connect(db_path),
        mapped_fn=lambda _ids: set(),
        clock=lambda: 1_000.0,
    )
    rows.present()
    warmed = rows.audio_hashes()
    assert warmed["a"] == warmed["b"] == same
    assert calls["n"] == 1

    runs: list[tuple[str, list[str]]] = []
    done = {lane: {"a"} for lane, _backend in aa.LANE_ORDER}

    def run_lane(lane: str, _backend: str, ids: list[str]) -> dict[str, str]:
        runs.append((lane, list(ids)))
        done[lane].update(ids)
        return {}

    drain = aa.AheadDrain(
        aa.AheadSources(
            present_fn=rows.present,
            mapped_fn=rows.mapped,
            has_strip_fn=lambda _sid: True,
            write_strip_fn=lambda _sid: None,
            done_fn=lambda lane, _backend: set(done[lane]),
            run_lane_fn=run_lane,
            playing_fn=lambda: False,
            blank_tags_fn=set,
            refresh_tags_fn=lambda _sid: True,
            declined_fn=lambda _lane, _backend: {},
            paths_fn=rows.paths,
            hashes_fn=rows.audio_hashes,
        )
    )
    return runs, drain, conn, calls, changed


def test_a_rewritten_hash_inside_the_refresh_window_is_not_skipped(tmp_path: Path) -> None:
    """[if] one duplicate's hash changes inside the refresh window [then] it is enqueued, [else stop]."""
    runs, drain, conn, calls, changed = _window_pair(tmp_path)
    try:
        conn.execute("UPDATE tracks SET audio_hash = ? WHERE stable_id = 'b'", (changed,))
        conn.commit()
        _run_to_green(drain)
        assert calls["n"] == 1, calls
        assert any("b" in ids for _lane, ids in runs), runs
    finally:
        conn.close()


def test_byte_identical_siblings_stay_skipped_inside_the_refresh_window(tmp_path: Path) -> None:
    """[if] hashes still match inside the refresh window [then] the sibling is not enqueued, [else stop]."""
    runs, drain, conn, calls, _changed = _window_pair(tmp_path)
    try:
        _run_to_green(drain)
        assert calls["n"] == 1, calls
        assert runs and all("b" not in ids for _lane, ids in runs), runs
        assert any("c" in ids for _lane, ids in runs), runs
    finally:
        conn.close()


def test_a_tombstoned_row_does_not_lend_its_hash(tmp_path: Path) -> None:
    """[if] a sibling row is tombstoned [then] its hash is not read, [else stop]."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, file_path TEXT, "
            "audio_hash TEXT, deleted_at TEXT)"
        )
        same = "sha256:" + "ab" * 32
        conn.executemany(
            "INSERT INTO tracks (stable_id, file_path, audio_hash, deleted_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("a", "/m/track.mp3", same, None),
                ("dead", "/m/track.mp3", same, "2026-10-08T00:00:00Z"),
            ],
        )
        conn.commit()
        rows = aa.PlayableRows(
            snapshot_fn=lambda: SimpleNamespace(
                on_disk=[("a", "/m/track.mp3"), ("dead", "/m/track.mp3")]
            ),
            open_ro=lambda: sqlite3.connect(db_path),
            mapped_fn=lambda _ids: set(),
        )
        assert rows.audio_hashes() == {"a": same}
    finally:
        conn.close()
