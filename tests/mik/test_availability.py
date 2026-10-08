"""``track_availability`` tests: the four states, the views, idempotency.

[if] a track is rowless, soft-deleted, or absent [then] safe-default views exclude it, [else stop].
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.mik import availability as avail
from apps.shared.platform_paths import load_path_map
from apps.shared.state.locations import upsert_location

pytestmark = pytest.mark.requirement("INFRA-01")


def test_present_when_the_file_resolves(tmp_path: Path) -> None:
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00")
    state, checked = avail.classify_path(str(audio))
    assert state == "present"
    assert checked == str(audio)


def test_absent_when_the_path_does_not_resolve(tmp_path: Path) -> None:
    state, checked = avail.classify_path(str(tmp_path / "gone.mp3"))
    assert state == "absent"
    assert checked is not None
    assert checked.endswith("gone.mp3")


def _write_path_map(data_dir: Path, from_prefix: Path, to_prefix: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "entries": [{"from": str(from_prefix), "to": str(to_prefix)}],
    }
    map_path = data_dir / "path-map.json"
    map_path.write_text(json.dumps(payload), encoding="utf-8")
    return data_dir


def test_present_when_raw_path_missing_but_path_map_target_exists(
    tmp_path: Path,
) -> None:
    stored_root = tmp_path / "stored" / "crate"
    mapped_root = tmp_path / "actual" / "crate"
    mapped_root.mkdir(parents=True)
    mapped_audio = mapped_root / "track.mp3"
    mapped_audio.write_bytes(b"\x00")
    raw = str(stored_root / "track.mp3")
    data_dir = _write_path_map(tmp_path / "data", stored_root, mapped_root)
    path_map = load_path_map(data_dir)
    state, checked = avail.classify_path(raw, path_map=path_map)
    assert state == "present"
    assert checked == str(mapped_audio)


def test_probe_batch_stores_present_when_raw_path_missing_but_path_map_target_exists(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    stored_root = tmp_path / "stored" / "crate"
    mapped_root = tmp_path / "actual" / "crate"
    mapped_root.mkdir(parents=True)
    mapped_audio = mapped_root / "track.mp3"
    mapped_audio.write_bytes(b"\x00")
    raw = str(stored_root / "track.mp3")
    data_dir = _write_path_map(tmp_path / "data", stored_root, mapped_root)
    path_map = load_path_map(data_dir)
    stable_id = "a" * 40
    add_track(stable_id, file_path=raw)
    rows = avail.probe_batch(state_conn, [stable_id], path_map=path_map)
    assert len(rows) == 1
    assert rows[0].state == "present"
    assert rows[0].checked_path == str(mapped_audio)
    avail.write(state_conn, rows)
    stored = state_conn.execute(
        "SELECT state, checked_path FROM track_availability WHERE stable_id = ?",
        (stable_id,),
    ).fetchone()
    assert stored == ("present", str(mapped_audio))


def test_deleted_location_path_map_target_does_not_count_as_present(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    """A tombstoned location is not a path the deck will open.

    [if] the stored path is missing and the only mapped file belongs to a
    soft-deleted track_locations row [then] the probe stays absent.
    [if] the same mapped file belongs to a live location [then] the probe
    marks that track present.
    """
    stored_root = tmp_path / "stored" / "crate"
    mapped_root = tmp_path / "actual" / "crate"
    mapped_root.mkdir(parents=True)
    mapped_audio = mapped_root / "tomb.mp3"
    mapped_audio.write_bytes(b"\x00")
    raw = str(stored_root / "tomb.mp3")
    assert not Path(raw).exists()
    data_dir = _write_path_map(tmp_path / "data", stored_root, mapped_root)
    path_map = load_path_map(data_dir)

    deleted_id = "d" * 40
    add_track(deleted_id, file_path="/nope/primary.mp3")
    upsert_location(state_conn, stable_id=deleted_id, kind="local", file_path=raw)
    state_conn.execute(
        "UPDATE track_locations SET deleted_at = ? WHERE stable_id = ?",
        ("2026-10-07T00:00:00+00:00", deleted_id),
    )
    deleted_rows = avail.probe_batch(state_conn, [deleted_id], path_map=path_map)
    assert len(deleted_rows) == 1
    assert deleted_rows[0].state == "absent"
    assert deleted_rows[0].checked_path == "/nope/primary.mp3"

    live_id = "e" * 40
    add_track(live_id, file_path="/nope/other.mp3")
    upsert_location(state_conn, stable_id=live_id, kind="local", file_path=raw)
    live_rows = avail.probe_batch(state_conn, [live_id], path_map=path_map)
    assert len(live_rows) == 1
    assert live_rows[0].state == "present"
    assert live_rows[0].checked_path == str(mapped_audio)


def test_remote_location_path_map_target_does_not_count_as_present(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    """Deck load ignores kind=remote rows, so the probe must too.

    [if] the legacy path is missing and the only mapped file belongs to a
    same-machine kind=remote track_locations row [then] the probe stays absent.
    [if] that remote row's file_path exists on disk [then] the probe still
    stays absent.
    """
    stored_root = tmp_path / "stored" / "crate"
    mapped_root = tmp_path / "actual" / "crate"
    mapped_root.mkdir(parents=True)
    mapped_audio = mapped_root / "remote.mp3"
    mapped_audio.write_bytes(b"\x00")
    raw = str(stored_root / "remote.mp3")
    assert not Path(raw).exists()
    data_dir = _write_path_map(tmp_path / "data", stored_root, mapped_root)
    path_map = load_path_map(data_dir)

    mapped_id = "f" * 40
    add_track(mapped_id, file_path="/nope/remote-primary.mp3")
    upsert_location(
        state_conn,
        stable_id=mapped_id,
        kind="remote",
        file_path=raw,
        remote_url="https://example.invalid/remote-mapped",
    )
    mapped_rows = avail.probe_batch(state_conn, [mapped_id], path_map=path_map)
    assert len(mapped_rows) == 1
    assert mapped_rows[0].state == "absent"
    assert mapped_rows[0].checked_path == "/nope/remote-primary.mp3"

    on_disk = tmp_path / "on-disk-remote.mp3"
    on_disk.write_bytes(b"\x00")
    disk_id = "g" * 40
    add_track(disk_id, file_path="/nope/remote-disk.mp3")
    upsert_location(
        state_conn,
        stable_id=disk_id,
        kind="remote",
        file_path=str(on_disk),
        remote_url="https://example.invalid/remote-disk",
    )
    disk_rows = avail.probe_batch(state_conn, [disk_id], path_map=path_map)
    assert len(disk_rows) == 1
    assert disk_rows[0].state == "absent"
    assert disk_rows[0].checked_path == "/nope/remote-disk.mp3"


def test_absent_when_raw_path_and_path_map_target_are_both_missing(
    tmp_path: Path,
) -> None:
    stored_root = tmp_path / "stored" / "crate"
    mapped_root = tmp_path / "actual" / "crate"
    mapped_root.mkdir(parents=True)
    raw = str(stored_root / "gone.mp3")
    data_dir = _write_path_map(tmp_path / "data", stored_root, mapped_root)
    path_map = load_path_map(data_dir)
    state, checked = avail.classify_path(raw, path_map=path_map)
    assert state == "absent"
    assert checked == raw


def test_present_raw_path_is_unchanged_when_a_path_map_entry_exists(
    tmp_path: Path,
) -> None:
    stored_root = tmp_path / "stored" / "crate"
    mapped_root = tmp_path / "actual" / "crate"
    stored_root.mkdir(parents=True)
    mapped_root.mkdir(parents=True)
    raw_audio = stored_root / "track.mp3"
    mapped_audio = mapped_root / "track.mp3"
    raw_audio.write_bytes(b"\x00")
    mapped_audio.write_bytes(b"\x00")
    data_dir = _write_path_map(tmp_path / "data", stored_root, mapped_root)
    path_map = load_path_map(data_dir)
    state, checked = avail.classify_path(str(raw_audio), path_map=path_map)
    assert state == "present"
    assert checked == str(raw_audio)


def test_unmounted_volume_is_awaiting_volume_not_absent() -> None:
    """Plug the drive in and it is present again, so a relocate pass must skip it."""
    state, _ = avail.classify_path(
        "/Volumes/SLATER/Contents/track.mp3", mounted=set()
    )
    assert state == "awaiting_volume"


def test_mounted_volume_with_missing_file_is_absent() -> None:
    state, _ = avail.classify_path(
        "/Volumes/SLATER/Contents/track.mp3", mounted={"SLATER"}
    )
    assert state == "absent"


@pytest.mark.parametrize(
    "uri",
    [
        "spotify:track:7moEQlb8TT7Xh3KHCSYNG9",
        "tidal:tracks:93736136",
        "soundcloud:tracks:1104342268",
    ],
)
def test_streaming_ids_are_streaming_not_absent(uri: str) -> None:
    """387 real rows are streaming ids. There is no file to be missing."""
    state, checked = avail.classify_path(uri)
    assert state == "streaming"
    assert checked == uri


def test_null_path_is_absent_with_no_checked_path() -> None:
    assert avail.classify_path(None) == ("absent", None)
    assert avail.classify_path("   ") == ("absent", None)


def test_refresh_is_idempotent(state_conn: sqlite3.Connection, add_track) -> None:
    add_track("a" * 40, file_path="/nope/a.mp3")
    add_track("b" * 40, file_path="spotify:track:xyz")
    first = avail.refresh(state_conn)
    second = avail.refresh(state_conn)
    assert first.changed == 2
    assert second.changed == 0
    assert second.unchanged == 2


def test_checked_at_only_moves_when_the_state_changes(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00")
    keeper = tmp_path / "keep.mp3"
    keeper.write_bytes(b"\x00")
    add_track("a" * 40, file_path=str(audio))
    add_track("k" * 40, file_path=str(keeper))
    avail.refresh(state_conn, now="2026-01-01T00:00:00+00:00")
    avail.refresh(state_conn, now="2026-02-02T00:00:00+00:00")
    stamp = state_conn.execute(
        "SELECT checked_at FROM track_availability WHERE stable_id = ?",
        ("a" * 40,),
    ).fetchone()[0]
    assert stamp == "2026-01-01T00:00:00+00:00"
    audio.unlink()
    avail.refresh(state_conn, now="2026-03-03T00:00:00+00:00")
    state, stamp = state_conn.execute(
        "SELECT state, checked_at FROM track_availability WHERE stable_id = ?",
        ("a" * 40,),
    ).fetchone()
    assert (state, stamp) == ("absent", "2026-03-03T00:00:00+00:00")


def test_counts_sum_to_the_track_count(
    state_conn: sqlite3.Connection, add_track
) -> None:
    """Bucket invariant: every track lands in exactly one bucket, including unknown."""
    for index in range(5):
        add_track(f"{index}" * 40, file_path=f"/nope/{index}.mp3")
    avail.refresh(state_conn)
    add_track("z" * 40, file_path="/nope/z.mp3")  # never probed
    counts = avail.counts(state_conn)
    total = state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert sum(counts.values()) == total
    assert counts["unknown"] == 1


def test_counts_excludes_a_soft_deleted_tombstone(
    state_conn: sqlite3.Connection, add_track
) -> None:
    """P2 regression (PR #383 review): a soft delete never issues a physical
    DELETE, so the tombstone's ``track_availability`` row survives it while
    ``total`` (``tracks WHERE deleted_at IS NULL``) already excludes the
    track. Left unfiltered, the histogram counts the tombstone and
    ``unknown`` can go negative."""
    add_track("a" * 40, file_path="/nope/a.mp3")
    avail.refresh(state_conn)
    state_conn.execute(
        "UPDATE tracks SET deleted_at = '2026-09-03T00:00:00+00:00' "
        "WHERE stable_id = ?",
        ("a" * 40,),
    )
    counts = avail.counts(state_conn)
    assert sum(counts.values()) == 0
    assert counts["unknown"] == 0


def test_views_exclude_unprobed_and_unavailable_rows(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    audio = tmp_path / "here.mp3"
    audio.write_bytes(b"\x00")
    add_track("a" * 40, file_path=str(audio))
    add_track("b" * 40, file_path="/nope/b.mp3")
    add_track("c" * 40, file_path="/nope/c.mp3")
    avail.refresh(state_conn)
    add_track("d" * 40, file_path="/nope/d.mp3")  # unknown: no availability row
    assert state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 4
    assert (
        state_conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0] == 1
    )
    assert (
        state_conn.execute("SELECT COUNT(*) FROM tracks_unavailable").fetchone()[0] == 2
    )


def test_write_rejects_an_unknown_state(state_conn: sqlite3.Connection, add_track) -> None:
    add_track("a" * 40, file_path="/nope/a.mp3")
    rows = [avail.AvailabilityRow(stable_id="a" * 40, state="lost", checked_path=None)]
    with pytest.raises(ValueError, match="not in"):
        avail.write(state_conn, rows)


def test_alternate_location_rescues_a_dead_legacy_path(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    """P1 regression (PR #383 review): a track whose legacy ``file_path`` is
    gone but that has a working ``track_locations`` alternate on this
    machine must probe as ``present``, not ``absent`` -- the audio endpoint's
    location picker already plays it through that alternate."""
    alt = tmp_path / "relocated.mp3"
    alt.write_bytes(b"\x00")
    add_track("a" * 40, file_path="/nope/a.mp3")
    upsert_location(
        state_conn, stable_id="a" * 40, kind="local", file_path=str(alt)
    )
    rows = avail.probe(state_conn)
    assert len(rows) == 1
    assert rows[0].state == "present"
    assert rows[0].checked_path == str(alt)


def test_legacy_path_wins_over_an_alternate_when_both_resolve(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    legacy = tmp_path / "legacy.mp3"
    legacy.write_bytes(b"\x00")
    alt = tmp_path / "alt.mp3"
    alt.write_bytes(b"\x00")
    add_track("a" * 40, file_path=str(legacy))
    upsert_location(state_conn, stable_id="a" * 40, kind="local", file_path=str(alt))
    rows = avail.probe(state_conn)
    assert rows[0].state == "present"
    assert rows[0].checked_path == str(legacy)


def test_awaiting_volume_alternate_is_preferred_over_absent(
    state_conn: sqlite3.Connection, add_track
) -> None:
    """P2 regression (PR #383 review): when the legacy path is absent but a
    ``track_locations`` alternate points under an unplugged ``/Volumes/<name>``,
    the alternate loop only accepted ``present`` and fell through to
    ``absent``. A re-acquisition query then treats an external copy that will
    come back with its volume as genuinely gone."""
    add_track("a" * 40, file_path="/nope/a.mp3")
    upsert_location(
        state_conn,
        stable_id="a" * 40,
        kind="local",
        file_path="/Volumes/SLATER/Music/a.mp3",
    )
    rows = avail.probe(state_conn)
    assert len(rows) == 1
    assert rows[0].state == "awaiting_volume"
    assert rows[0].checked_path == "/Volumes/SLATER/Music/a.mp3"


def test_safe_views_exclude_a_soft_deleted_track(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    """P1 regression (PR #383 review): a soft delete only sets
    ``tracks.deleted_at``, it never issues a physical DELETE, so the
    ``track_availability`` FK's ``ON DELETE CASCADE`` never fires and a
    tombstoned track's availability row (and track_fields row) survives it.
    The safe-default views must exclude it explicitly rather than resurrect
    a synced ghost or inflate an aggregate."""
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00")
    add_track("a" * 40, file_path=str(audio))
    state_conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "modified_at) VALUES (?, 'bpm', '120.0', 'rekordbox', "
        "'2026-01-01T00:00:00+00:00')",
        ("a" * 40,),
    )
    avail.refresh(state_conn)
    assert state_conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0] == 1
    assert (
        state_conn.execute("SELECT COUNT(*) FROM track_fields_available").fetchone()[0]
        == 1
    )

    state_conn.execute(
        "UPDATE tracks SET deleted_at = '2026-09-03T00:00:00+00:00' "
        "WHERE stable_id = ?",
        ("a" * 40,),
    )
    assert (
        state_conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE stable_id = ?", ("a" * 40,)
        ).fetchone()[0]
        == 1
    ), "the FK cascade never fires on a soft delete; the row must still be there"
    assert state_conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0] == 0
    assert (
        state_conn.execute("SELECT COUNT(*) FROM tracks_unavailable").fetchone()[0] == 0
    )
    assert (
        state_conn.execute("SELECT COUNT(*) FROM track_fields_available").fetchone()[0]
        == 0
    )


def test_safe_views_exclude_a_soft_deleted_field_row(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    """The sibling omission named in the same review thread: a tombstoned
    ``track_fields`` row on a live track must not appear in
    ``track_fields_available`` either."""
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00")
    add_track("a" * 40, file_path=str(audio))
    state_conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "modified_at, deleted_at) VALUES (?, 'bpm', '120.0', 'rekordbox', "
        "'2026-01-01T00:00:00+00:00', '2026-09-03T00:00:00+00:00')",
        ("a" * 40,),
    )
    avail.refresh(state_conn)
    assert state_conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0] == 1
    assert (
        state_conn.execute("SELECT COUNT(*) FROM track_fields_available").fetchone()[0]
        == 0
    )


def test_partial_batch_write_skips_mass_missing_guard(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    """Engine-sized batches must not compare a local batch with the whole library."""
    one = tmp_path / "one.mp3"
    two = tmp_path / "two.mp3"
    one.write_bytes(b"\x00")
    two.write_bytes(b"\x00")
    add_track("a" * 40, file_path=str(one))
    add_track("b" * 40, file_path=str(two))
    avail.refresh(state_conn)
    one.unlink()
    rows = avail.probe_batch(state_conn, ["a" * 40])
    report = avail.write(state_conn, rows, apply_mass_missing_guard=False)
    assert report.changed == 1
    state = state_conn.execute(
        "SELECT state FROM track_availability WHERE stable_id = ?", ("a" * 40,)
    ).fetchone()[0]
    assert state == "absent"


def test_round_present_drop_refuses_a_background_batch(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    from apps.shared.scan_mass_missing import MassMissingError
    from apps.shared.state.availability_write import guard_round_present_drop

    paths: list[Path] = []
    for index in range(10):
        audio = tmp_path / f"{index}.mp3"
        audio.write_bytes(b"\x00")
        paths.append(audio)
        add_track(f"{index}" * 40, file_path=str(audio))
    avail.refresh(state_conn)
    for audio in paths[:6]:
        audio.unlink()
    rows = avail.probe_batch(state_conn, [f"{index}" * 40 for index in range(10)])
    with pytest.raises(MassMissingError, match="library-round"):
        guard_round_present_drop(state_conn, rows, round_start_present=10)


def test_probe_batch_matches_probe_subset(
    state_conn: sqlite3.Connection, add_track, tmp_path: Path
) -> None:
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00")
    add_track("a" * 40, file_path=str(audio))
    add_track("b" * 40, file_path="/nope/b.mp3")
    full = {row.stable_id: row for row in avail.probe(state_conn)}
    batch = avail.probe_batch(state_conn, ["a" * 40])
    assert len(batch) == 1
    assert batch[0] == full["a" * 40]


def test_resolve_data_dir_rejects_a_non_directory(tmp_path: Path) -> None:
    bogus = tmp_path / "not-a-dir"
    bogus.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        avail.resolve_data_dir(str(bogus))
