"""Tests for the vendor-unmapped analysis backlog (apps.analysis.backlog).

Real state.db built through the production migrations; the backlog is read
with the same SQL the daemon runs, resolved through the production
``platform_paths.resolve_library_path`` and probed by the production
``fs_residency.is_materialised``. Nothing is injected.

UNAVAILABLE here: the dataless-placeholder branch. ``fs_residency`` returns
False from ``is_dataless_stub`` on every non-darwin platform
(``fs_residency.py:28-30``), so an iCloud stub has no representation on this
box and manufacturing the probe's answer would test the stub rather than the
classifier. That branch is owned by
``tests/shared/test_fs_residency.py::test_sparse_nonzero_zero_blocks_is_dataless_on_darwin``;
what this module covers is the queue's use of the real classifier, with a
real materialized file and a real missing one.

Regression lines:
  - if a rekordbox-mapped track appears in the backlog then broken
  - if a soft-deleted vendor row keeps a track out of the backlog then broken
  - if an already-analyzed unmapped track is queued again then broken
  - if a row from a backend the drain does not run counts as analyzed then broken
  - if an in-place file repair does not move the change signature then broken
  - if a streaming URI row is queued for local analysis then broken
  - if an http(s) row is bucketed unreachable instead of excluded then broken
  - if an empty file_path is probed as the current directory then broken
  - if a non-materialized file is queued instead of counted unreachable then broken
  - if a path-mapped track is probed at its raw stored path then broken
  - if a queue item carries a path the analyzer cannot decode then broken
  - if a never-analyzed library raises instead of reporting every track pending
    then broken
  - if the signature does not change when the pending set changes then broken
  - if a display limit shrinks the reported queue size or signature then broken
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from apps.analysis import backlog as backlog_mod
from apps.analysis import store as analysis_store
from apps.shared.state.db import open_rw as open_state_rw

_TS = "2026-09-01T00:00:00Z"


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "state.db"
    open_state_rw(path).close()
    return path


def _add_track(db: Path, sid: str, file_path: str | Path, *, deleted: bool = False) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
        "file_path, created_at, updated_at, deleted_at) VALUES (?,?,?,?,?,?,?,?)",
        (sid, "inferred", f"title-{sid}", "[]", str(file_path), _TS, _TS,
         _TS if deleted else None),
    )
    conn.commit()
    conn.close()


def _add_vendor(db: Path, sid: str, *, vendor: str = "rekordbox", deleted: bool = False) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id, deleted_at) "
        "VALUES (?,?,?,?)",
        (sid, vendor, f"vid-{sid}", _TS if deleted else None),
    )
    conn.commit()
    conn.close()


def _add_location(db: Path, sid: str, file_path: str | Path) -> None:
    """Record an extra playable copy the way the production writer does.

    ``upsert_track_location`` deliberately leaves ``tracks.file_path`` alone,
    so this is the state a hydrate or a sync leaves behind: the legacy column
    is null or stale while THIS machine has a real file.
    """
    from apps.shared.state import locations as locations_mod
    from apps.shared.state import sync_stamp

    conn = sqlite3.connect(db)
    try:
        locations_mod.upsert_location(
            conn, stable_id=sid, kind="local", file_path=str(file_path),
            role="alternate", now=_TS,
            machine_id=sync_stamp.local_machine_id(conn),
        )
        conn.commit()
    finally:
        conn.close()


def _add_analysis(db: Path, sid: str, *, backend: str = "librosa") -> None:
    conn = analysis_store.open_conn(db)
    conn.execute(
        "INSERT INTO analysis (stable_id, backend, backend_version, analyzed_at, "
        "duration_s, sample_rate, bpm, bpm_confidence, key_camelot, key_openkey, "
        "key_confidence, energy, energy_source, record_json) VALUES "
        "(?, ?, 'seed', ?, 200.0, 44100, 120.0, 0.9, '8A', '1d', 0.9, 5, "
        "'seed', '{}')",
        (sid, backend, _TS),
    )
    conn.commit()
    conn.close()


def _scan(db: Path, **kwargs):
    conn = sqlite3.connect(db)
    try:
        return backlog_mod.scan(conn, **kwargs)
    finally:
        conn.close()


def _audio(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x00" * 4096)
    return p


# ----- membership -----------------------------------------------------------

@pytest.mark.requirement("PARITY-06")
def test_mapped_track_is_never_queued(state_db, tmp_path):
    mapped = _audio(tmp_path, "mapped.mp3")
    local = _audio(tmp_path, "local.mp3")
    _add_track(state_db, "mapped01", mapped)
    _add_vendor(state_db, "mapped01")
    _add_track(state_db, "local001", local)

    out = _scan(state_db)
    assert [i.stable_id for i in out.pending] == ["local001"]
    assert out.unmapped == 1


@pytest.mark.requirement("PARITY-06")
def test_a_non_rekordbox_mapping_does_not_count_as_a_mapping(state_db, tmp_path):
    """Only a rekordbox mapping supplies an ANLZ. A djay-, serato- or
    traktor-only row (the agentbox crate replication writes whatever vendor the
    payload names) leaves the track with no beatgrid, waveform or cues to read,
    which is exactly the population this queue exists for. The browser agrees:
    rb_vendor_pkg/track_rows.py sets has_rb_mapping from a
    ``vendor = 'rekordbox'`` lookup and names a djay-only mapping as a real
    library state."""
    _add_track(state_db, "djay0001", _audio(tmp_path, "d.mp3"))
    _add_vendor(state_db, "djay0001", vendor="djay")
    _add_track(state_db, "srto0001", _audio(tmp_path, "s.mp3"))
    _add_vendor(state_db, "srto0001", vendor="serato")
    _add_track(state_db, "both0001", _audio(tmp_path, "b.mp3"))
    _add_vendor(state_db, "both0001", vendor="djay")
    _add_vendor(state_db, "both0001", vendor="rekordbox")

    out = _scan(state_db)
    assert [i.stable_id for i in out.pending] == ["djay0001", "srto0001"]
    assert out.unmapped == 2, "a rekordbox mapping is the only one that excludes"


def test_soft_deleted_vendor_row_makes_the_track_unmapped_again(state_db, tmp_path):
    _add_track(state_db, "gone0001", _audio(tmp_path, "a.mp3"))
    _add_vendor(state_db, "gone0001", deleted=True)

    out = _scan(state_db)
    assert [i.stable_id for i in out.pending] == ["gone0001"]


def test_soft_deleted_track_is_excluded(state_db, tmp_path):
    _add_track(state_db, "dead0001", _audio(tmp_path, "a.mp3"), deleted=True)

    out = _scan(state_db)
    assert out.pending == ()
    assert out.unmapped == 0


@pytest.mark.requirement("PARITY-06")
def test_analyzed_unmapped_track_counts_analyzed_not_pending(state_db, tmp_path):
    _add_track(state_db, "done0001", _audio(tmp_path, "a.mp3"))
    _add_analysis(state_db, "done0001")

    out = _scan(state_db)
    assert out.pending == ()
    assert out.analyzed == 1
    assert out.unmapped == 1


@pytest.mark.requirement("PARITY-06")
def test_a_row_from_another_backend_does_not_count_as_analyzed(state_db, tmp_path):
    """The drain shells out to ``apps.analysis.run`` with no --backend, so it
    runs DEFAULT_BACKEND and its own filter_missing() drops a track only when a
    row exists for THAT backend. A mik row is not that: MikBackend copies
    whatever the CLI emitted, downbeats included, and an empty downbeats list
    makes /beatgrid-fallback 404 with BEATGRID_FALLBACK_NOT_FOUND. Counting it
    analyzed would leave the track permanently without a grid while the queue
    reported it done."""
    _add_track(state_db, "mikk0001", _audio(tmp_path, "m.mp3"))
    _add_analysis(state_db, "mikk0001", backend="mik")
    _add_track(state_db, "libr0001", _audio(tmp_path, "l.mp3"))
    _add_analysis(state_db, "libr0001", backend=backlog_mod.DRAIN_BACKEND)

    out = _scan(state_db)
    assert [i.stable_id for i in out.pending] == ["mikk0001"]
    assert out.analyzed == 1
    assert out.unmapped == 2


def test_streaming_uri_row_is_excluded_from_every_count(state_db):
    _add_track(state_db, "strm0001", "spotify:track:abc")
    _add_track(state_db, "strm0002", "tidal:track:def")
    _add_track(state_db, "strm0003", "soundcloud:track:ghi")

    out = _scan(state_db)
    assert out.pending == ()
    assert out.unmapped == 0
    assert out.unreachable == 0


@pytest.mark.requirement("PARITY-06")
def test_unmaterialized_file_counts_unreachable_not_pending(state_db, tmp_path):
    here = _audio(tmp_path, "here.mp3")
    _add_track(state_db, "here0001", here)
    _add_track(state_db, "gone0002", tmp_path / "missing.mp3")

    out = _scan(state_db)
    assert [i.stable_id for i in out.pending] == ["here0001"]
    assert out.unreachable == 1
    assert out.unmapped == 2


@pytest.mark.requirement("PARITY-06")
def test_a_path_mapped_track_is_queued_at_its_local_path(state_db, tmp_path, monkeypatch):
    """agentbox and Windows read a library whose stored paths are a Mac's.

    ``platform_paths.resolve_library_path`` is the ONE resolver the webui
    backend and the vocals CLI already go through, and ``MDT_PATH_MAP`` is
    the real config that drives it. Probing the raw stored path instead would
    count every track on those deployments as unreachable and never analyze
    any of them, while the mapped file sits materialized on disk.

    The queue item must also CARRY the resolved path, because that string is
    what ``--pairs-json`` hands the analyzer to decode.
    """
    local = _audio(tmp_path, "mapped.mp3")
    map_file = tmp_path / "path-map.json"
    map_file.write_text(
        json.dumps({"entries": [{"from": "/Users/user/Music", "to": str(tmp_path)}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))
    _add_track(state_db, "mapd0001", "/Users/user/Music/mapped.mp3")

    out = _scan(state_db)
    assert [i.stable_id for i in out.pending] == ["mapd0001"]
    assert out.pending[0].file_path == str(local)
    assert out.unreachable == 0


@pytest.mark.requirement("PARITY-06")
def test_an_unmappable_foreign_path_counts_unreachable(state_db, tmp_path, monkeypatch):
    """No map entry for a stored path this machine cannot resolve.

    ``resolved is None`` is the resolver's explicit "not on this machine"
    state. It is a real library condition with a real repair (add a path-map
    entry), so it belongs in ``unreachable`` rather than being dropped.
    """
    map_file = tmp_path / "path-map.json"
    map_file.write_text(json.dumps({"entries": []}), encoding="utf-8")
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))
    monkeypatch.setenv("MDT_LIBRARY_MODE", "remote")
    _add_track(state_db, "frgn0001", "/Users/user/Music/nowhere.mp3")

    out = _scan(state_db)
    assert out.pending == ()
    assert out.unmapped == 1
    assert out.unreachable == 1


def test_an_http_streaming_row_is_excluded_like_every_other_scheme(state_db):
    """The scheme set is the repo's, not this module's.

    ``rb_vendor``'s subsonic/stream rows land with an ``https://`` FolderPath.
    Before the canonical predicate was reused here, those rows failed the
    materialization probe and were reported as ``unreachable`` -- inviting a
    user to "repair" a URL that was never a file.
    """
    _add_track(state_db, "http0001", "http://stream.example/track.mp3")
    _add_track(state_db, "http0002", "https://stream.example/track.mp3")

    out = _scan(state_db)
    assert out.pending == ()
    assert out.unmapped == 0
    assert out.unreachable == 0


def test_an_empty_file_path_is_excluded_rather_than_probed_as_the_cwd(state_db):
    """A pathless track is not a broken file, so it is not the queue's work.

    ``Path("")`` is ``Path(".")`` -- the current directory -- so an empty
    path gets probed as if it named audio. Today that lands the row in
    ``unreachable``, inflating the count of files a user is told to repair
    with one that never had a path to lose. ``is_unplayable_path`` answers
    True for the empty string precisely so callers stop asking the
    filesystem about it.
    """
    _add_track(state_db, "nopath01", "")

    out = _scan(state_db)
    assert out.pending == ()
    assert out.unmapped == 0
    assert out.unreachable == 0


def test_null_file_path_is_excluded(state_db):
    conn = sqlite3.connect(state_db)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
        "file_path, created_at, updated_at) VALUES (?,?,?,?,NULL,?,?)",
        ("nofp0001", "inferred", "t", "[]", _TS, _TS),
    )
    conn.commit()
    conn.close()

    out = _scan(state_db)
    assert out.pending == ()
    assert out.unmapped == 0


# ----- never-analyzed library ----------------------------------------------

def test_never_analyzed_library_reports_pending_not_an_error(state_db, tmp_path):
    """apps.analysis.store creates its table on first write, so a library
    that has never been analyzed legitimately has no ``analysis`` table."""
    _add_track(state_db, "fresh001", _audio(tmp_path, "a.mp3"))
    tables = {
        r[0] for r in sqlite3.connect(state_db).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "analysis" not in tables, "fixture precondition: no analysis table yet"

    out = _scan(state_db)
    assert [i.stable_id for i in out.pending] == ["fresh001"]
    assert out.analyzed == 0


# ----- ordering, limit, signature -------------------------------------------

def test_pending_is_deterministically_ordered_and_limitable(state_db, tmp_path):
    for sid in ("c0000001", "a0000001", "b0000001"):
        _add_track(state_db, sid, _audio(tmp_path, f"{sid}.mp3"))

    out = _scan(state_db)
    assert [i.stable_id for i in out.pending] == ["a0000001", "b0000001", "c0000001"]
    capped = _scan(state_db, limit=2)
    assert [i.stable_id for i in capped.pending] == ["a0000001", "b0000001"]
    assert capped.unmapped == 3, "a display limit must not change the denominator"
    assert capped.pending_total == 3, "a paged read must report the real queue size"
    assert capped.signature == out.signature, "a paged read must agree on identity"


@pytest.mark.requirement("PARITY-06")
def test_buckets_always_sum_to_the_unmapped_population(state_db, tmp_path):
    _add_track(state_db, "pend0001", _audio(tmp_path, "p.mp3"))
    _add_track(state_db, "anal0001", _audio(tmp_path, "d.mp3"))
    _add_analysis(state_db, "anal0001")
    _add_track(state_db, "unre0001", tmp_path / "nope.mp3")
    _add_track(state_db, "mapd0001", _audio(tmp_path, "m.mp3"))
    _add_vendor(state_db, "mapd0001")

    out = _scan(state_db)
    assert out.unmapped == 3
    assert out.analyzed + out.unreachable + out.pending_total == out.unmapped


def test_negative_limit_is_rejected(state_db):
    with pytest.raises(ValueError, match="limit must be >= 0"):
        _scan(state_db, limit=-1)


def test_signature_tracks_the_pending_set(state_db, tmp_path):
    _add_track(state_db, "sig00001", _audio(tmp_path, "a.mp3"))
    first = _scan(state_db).signature
    assert first == _scan(state_db).signature, "signature must be stable"

    _add_track(state_db, "sig00002", _audio(tmp_path, "b.mp3"))
    assert _scan(state_db).signature != first


def test_signature_tracks_a_relink_that_keeps_the_stable_id(state_db, tmp_path):
    """A failed decode that is repaired by relinking the row to a good file
    keeps its stable_id, so an id-only digest would report the repaired queue
    as unchanged and the auto-drain would never retry it."""
    _add_track(state_db, "relnk001", _audio(tmp_path, "broken.mp3"))
    before = _scan(state_db).signature

    repaired = _audio(tmp_path, "repaired.mp3")
    conn = sqlite3.connect(state_db)
    conn.execute("UPDATE tracks SET file_path = ? WHERE stable_id = ?",
                 (str(repaired), "relnk001"))
    conn.commit()
    conn.close()

    after = _scan(state_db)
    assert [i.file_path for i in after.pending] == [str(repaired)]
    assert after.signature != before, "a repaired input must re-arm the drain"


def test_signature_tracks_an_in_place_repair_at_the_same_path(state_db, tmp_path):
    """A file repaired IN PLACE keeps both its stable_id and its path, so a
    signature over identity alone would call the queue unchanged and never
    retry bytes that just became decodable."""
    broken = _audio(tmp_path, "inplace.mp3")
    _add_track(state_db, "inpl0001", broken)
    before = _scan(state_db).signature

    broken.write_bytes(b"\x01" * 8192)  # different size: no clock resolution
    after = _scan(state_db).signature

    assert after != before, "repaired bytes must re-arm the drain"


def test_signature_tracks_a_repair_that_restores_size_and_mtime(state_db, tmp_path):
    """The nastiest repair shape: same length, same mtime, different bytes.

    A metadata-preserving rewrite (``cp -p`` over the original, a tag editor
    that stats first and calls ``os.utime`` after) leaves size and mtime_ns
    exactly as the drain last read them while the bytes underneath have
    changed completely. A token over size and mtime alone therefore reports
    an unchanged queue and the repaired file is never retried.

    The rewrite here is real, not simulated: the bytes on disk really differ
    and the mtime really is restored to the value the first scan read, so the
    two stat fields the old token used really are identical afterwards. That
    identity is asserted, so the test cannot pass by accident on a machine
    whose clock granularity happened to move the mtime anyway.
    """
    repaired = _audio(tmp_path, "preserved.mp3")
    _add_track(state_db, "keep0001", repaired)
    before_stat = repaired.stat()
    before = _scan(state_db).signature

    repaired.write_bytes(b"\x02" * before_stat.st_size)   # same length, new bytes
    os.utime(repaired, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))

    after_stat = repaired.stat()
    assert after_stat.st_size == before_stat.st_size, (
        "fixture precondition: the rewrite must not change the length"
    )
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns, (
        "fixture precondition: the mtime must be restored exactly, or this "
        "test would pass without discriminating anything"
    )

    assert _scan(state_db).signature != before, (
        "a repair that preserves size and mtime left the queue reading "
        "unchanged, so the drain will never retry the repaired bytes"
    )


#: The historical SQLite cap on host parameters per statement. Still the
#: compiled-in value on pre-3.32 builds, which includes the sqlite3 shipped
#: with some system Pythons and with the packaged desktop app.
_LEGACY_VARIABLE_LIMIT = 999


def test_a_folder_import_larger_than_the_sqlite_bind_limit_still_scans(state_db):
    """A big import must not take the queue endpoint down with it.

    The scan reads every unmapped track's locations in one bulk call. Built
    as a single ``IN (?, ?, ...)`` it binds one parameter per track, so a
    folder import past the connection's SQLITE_LIMIT_VARIABLE_NUMBER raises
    "too many SQL variables" - and it raises inside the scan, which means
    GET /analysis-queue AND every watcher tick fail outright rather than the
    library merely draining slowly.

    The limit is real, not simulated: `Connection.setlimit` sets the same
    knob to the same value pre-3.32 SQLite is compiled with, so this is the
    production code meeting a production configuration. Sizing the fixture
    past 32766 instead would take the test minutes to prove the same thing,
    and would still say nothing about the machines that actually ship 999.
    """
    conn = sqlite3.connect(state_db)
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, _LEGACY_VARIABLE_LIMIT)
        rows = [
            (f"bulk{i:04d}", "inferred", f"title-{i}", "[]",
             f"/nowhere/{i}.mp3", _TS, _TS, None)
            for i in range(_LEGACY_VARIABLE_LIMIT + 200)
        ]
        conn.executemany(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, "
            "artists_json, file_path, created_at, updated_at, deleted_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()

        out = backlog_mod.scan(conn)
    finally:
        conn.close()

    assert out.unmapped == len(rows), (
        "the scan lost tracks past the bind limit rather than raising, which "
        "would be worse: a silently short backlog drains and looks finished"
    )
    assert out.unreachable == len(rows)


def test_empty_backlog_signature_is_empty(state_db):
    assert _scan(state_db).signature == ""


def test_item_carries_the_path_and_title_the_runner_needs(state_db, tmp_path):
    path = _audio(tmp_path, "titled.mp3")
    _add_track(state_db, "titl0001", path)

    item = _scan(state_db).pending[0]
    assert item.file_path == str(path)
    assert item.title == "title-titl0001"


def test_a_track_whose_only_path_is_a_local_location_row_is_queued(state_db, tmp_path):
    """A null legacy path plus a real local copy is a playable track.

    ``upsert_track_location`` does not write ``tracks.file_path``, so after a
    hydrate or a sync a track can be locally playable with that column empty.
    The browser already treats it as present (``rb_vendor_pkg/track_rows.py``
    ORs the legacy path with the location paths) and ``resolve_playable_audio``
    will stream it, so leaving it out of the drain means a track the user can
    play never gets a beatgrid.
    """
    audio = tmp_path / "only-location.mp3"
    audio.write_bytes(b"\x00" * 2048)
    _add_track(state_db, "loc0001", "")
    _add_location(state_db, "loc0001", audio)

    with sqlite3.connect(state_db) as conn:
        result = backlog_mod.scan(conn)

    assert [item.stable_id for item in result.pending] == ["loc0001"]
    assert result.pending[0].file_path == str(audio), (
        "the queue item must carry the location path, since that string is "
        "what --pairs-json hands the analyzer to decode"
    )
    assert result.unreachable == 0


def test_a_stale_legacy_path_falls_back_to_the_local_location(state_db, tmp_path):
    """A stale legacy path must not hide a copy that is really here."""
    audio = tmp_path / "moved.mp3"
    audio.write_bytes(b"\x00" * 2048)
    _add_track(state_db, "loc0002", tmp_path / "gone" / "moved.mp3")
    _add_location(state_db, "loc0002", audio)

    with sqlite3.connect(state_db) as conn:
        result = backlog_mod.scan(conn)

    assert result.unreachable == 0, (
        "the track was counted unreachable at its stale legacy path while a "
        "materialized local copy was sitting in track_locations"
    )
    assert [item.file_path for item in result.pending] == [str(audio)]
