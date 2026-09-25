"""track_locations picker: local>remote, works>broken, venue window."""
from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import locations
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("INFRA-01")

SID = "d" * 40


def _init_track(conn, file_path: str | None = None) -> StateWriter:
    writer = StateWriter(conn, actor="unit-test")
    writer.upsert_track(
        stable_id=SID,
        stable_id_tier="inferred",
        title="Demo",
        artists=["X"],
        album=None,
        isrc=None,
        duration_ms=180_000,
        file_path=file_path,
    )
    return writer


def _flac(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC-not-a-real-frame-but-a-real-file")
    return path


def test_pick_prefers_working_local_over_remote(state_conn, tmp_path: Path) -> None:
    local = _flac(tmp_path / "local.flac")
    remote = _flac(tmp_path / "remote.flac")
    writer = _init_track(state_conn, file_path=str(local))
    try:
        writer.upsert_track_location(
            stable_id=SID, kind="remote", file_path=str(remote),
        )
        picked = locations.pick_playable(state_conn, SID)
    finally:
        writer.close()
    assert picked is not None
    assert picked.path == local
    assert picked.kind == "local"


def test_pick_skips_broken_and_uses_working_alternate(
    state_conn, tmp_path: Path,
) -> None:
    missing = tmp_path / "gone.flac"
    working = _flac(tmp_path / "ok.flac")
    writer = _init_track(state_conn, file_path=str(missing))
    try:
        writer.upsert_track_location(
            stable_id=SID, kind="local", file_path=str(working),
        )
        picked = locations.pick_playable(state_conn, SID)
    finally:
        writer.close()
    assert picked is not None
    assert picked.path == working


def test_pick_share_window_prefers_lossy_when_both_work(
    state_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lossless = _flac(tmp_path / "master.flac")
    lossy = tmp_path / "radio.mp3"
    # 1s at 320 kbps effective -> Warehouse (lossy ceiling).
    lossy.write_bytes(b"ID3" + bytes(40_000))
    writer = StateWriter(state_conn, actor="unit-test")
    writer.upsert_track(
        stable_id=SID,
        stable_id_tier="inferred",
        title="Demo",
        artists=["X"],
        album=None,
        isrc=None,
        duration_ms=1000,
        file_path=str(lossless),
    )
    try:
        writer.upsert_track_location(
            stable_id=SID, kind="local", file_path=str(lossy),
        )
        monkeypatch.setenv("MDT_AUDIO_SHARE_MAX_VENUE", "warehouse")
        picked = locations.pick_playable(
            state_conn, SID, policy=locations.policy_from_env(share=True),
        )
    finally:
        writer.close()
    assert picked is not None
    assert picked.path == lossy
    assert picked.venue_key == "warehouse"


def test_pick_returns_none_when_nothing_works(state_conn, tmp_path: Path) -> None:
    writer = _init_track(state_conn, file_path=str(tmp_path / "missing.flac"))
    try:
        assert locations.pick_playable(state_conn, SID) is None
    finally:
        writer.close()


def test_pick_accepts_an_explicit_replica_ledger_path(
    state_conn, tmp_path: Path,
) -> None:
    missing_owner_path = tmp_path / "owner-missing.flac"
    replica = _flac(tmp_path / "crate" / "indexed.flac")
    writer = _init_track(state_conn, file_path=str(missing_owner_path))
    try:
        picked = locations.pick_playable(
            state_conn,
            SID,
            extra_paths=((str(replica), "crate-index", "local"),),
        )
    finally:
        writer.close()

    assert picked is not None
    assert picked.path == replica
    assert picked.source == "crate-index"


def test_unknown_venue_env_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDT_AUDIO_MIN_VENUE", "disco")
    with pytest.raises(locations.LocationError, match="unknown venue"):
        locations.policy_from_env()


def test_fresh_db_migrates_locations_table(state_db_path: Path) -> None:
    conn = state_db.open_rw(state_db_path)
    try:
        assert locations.locations_table_ready(conn)
    finally:
        conn.close()


# ----- round 2 finding N2: one file, two Unicode spellings ------------------


def test_two_unicode_spellings_of_one_path_are_one_row(
    state_conn, tmp_path: Path
) -> None:
    """Round 2 finding N2, reproduced as a permanent regression.

    macOS hands back NFD from the filesystem, rekordbox and the Windows/Linux
    machines hand back NFC. Before the storage-boundary normalization those
    were two ``track_locations`` rows on ONE machine, and the partial UNIQUE
    index agreed because they are different bytes. Both canonicalized to the
    same NFC bytes on the wire, so the hub collapsed them to one and pruned
    the loser; the spoke kept two:

      [observed] n4a spoke rows for ONE file: 2
      [observed] n4a sync -> SyncDigestMismatch: tables ['track_locations']
      [observed] n4a hub rows: 1

    Permanent, three retries, no repair path.
    """
    nfc_name = unicodedata.normalize("NFC", "café-résumé.flac")
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    assert nfc_name != nfd_name, "the two spellings must differ as bytes"
    nfd_path = str(tmp_path / nfd_name)
    nfc_path = str(tmp_path / nfc_name)

    writer = _init_track(state_conn)
    try:
        first = writer.upsert_track_location(
            stable_id=SID, kind="local", file_path=nfd_path,
        )
        second = writer.upsert_track_location(
            stable_id=SID, kind="local", file_path=nfc_path,
        )
    finally:
        writer.close()

    assert first == second, (
        "the NFC spelling must resolve to the row the NFD spelling created, "
        "not mint a second location_id"
    )
    rows = state_conn.execute(
        "SELECT file_path FROM track_locations WHERE stable_id = ?", (SID,)
    ).fetchall()
    assert len(rows) == 1, (
        f"one file, {len(rows)} rows: the hub will collapse them and this "
        f"machine will fail its digest compare on track_locations forever"
    )
    assert rows[0][0] == nfc_path, "storage settles on the NFC spelling"


def test_a_remote_url_is_normalized_at_the_storage_boundary(
    state_conn, tmp_path: Path
) -> None:
    """Same root as N2, one column over. The wire NFC-normalizes every non-pk
    string, so storage that does not would disagree with the digest."""
    nfc_url = unicodedata.normalize("NFC", "https://example/café.flac")
    nfd_url = unicodedata.normalize("NFD", nfc_url)
    writer = _init_track(state_conn)
    try:
        first = writer.upsert_track_location(
            stable_id=SID, kind="remote", remote_url=nfd_url,
        )
        second = writer.upsert_track_location(
            stable_id=SID, kind="remote", remote_url=nfc_url,
        )
    finally:
        writer.close()
    assert first == second
    assert state_conn.execute(
        "SELECT COUNT(*) FROM track_locations WHERE stable_id = ? "
        "AND kind = 'remote'",
        (SID,),
    ).fetchone()[0] == 1


def test_normalize_stored_text_is_idempotent_and_passes_none_through() -> None:
    nfd = unicodedata.normalize("NFD", "café")
    once = locations.normalize_stored_text(nfd)
    assert once == unicodedata.normalize("NFC", "café")
    assert locations.normalize_stored_text(once) == once
    assert locations.normalize_stored_text(None) is None


@pytest.mark.requirement("LIBM-128")
def test_bulk_local_audio_paths_answers_what_the_per_id_reader_does(
    state_conn, tmp_path: Path,
) -> None:
    """[if] the listing batches path resolution
    [then] every id resolves as local_audio_path does, [else stop].

    Includes the one shape where the two used to differ: an id whose FIRST
    ordered local location has an empty path. The per-id reader takes that
    row (LIMIT 1) and drops it; the bulk reader used to fall through to the
    next location instead, so the listing could report a file its own
    single-track routes cannot find.
    """
    present = _flac(tmp_path / "present.flac")
    via_location = _flac(tmp_path / "via-location.flac")
    # A path is one location row per machine, so c's fallback is its own file.
    c_fallback = _flac(tmp_path / "c-fallback.flac")
    shapes = {
        "a" * 40: str(present),
        "b" * 40: str(tmp_path / "moved.flac"),
        "c" * 40: str(tmp_path / "missing.flac"),
        "e" * 40: "spotify:track:1",
        "f" * 40: None,
    }
    # Raw inserts: upsert_track would add a primary location mirroring
    # file_path, and the per-id reader only ever considers the FIRST location.
    state_conn.executemany(
        "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, file_path, "
        "created_at, updated_at) VALUES (?, 'inferred', 1000, ?, '2026-09-25', '2026-09-25')",
        list(shapes.items()),
    )
    state_conn.commit()
    writer = StateWriter(state_conn, actor="unit-test")
    try:
        writer.upsert_track_location(stable_id="b" * 40, kind="local", file_path=str(via_location))
        empty_first = writer.upsert_track_location(
            stable_id="c" * 40, kind="local", file_path=str(tmp_path / "placeholder.flac"),
            role="primary",
        )
        writer.upsert_track_location(stable_id="c" * 40, kind="local", file_path=str(c_fallback))
    finally:
        writer.close()
    # The schema CHECK admits an empty path only beside a remote_url.
    state_conn.execute(
        "UPDATE track_locations SET file_path = '', remote_url = 'https://example.invalid/c' "
        "WHERE location_id = ?",
        (empty_first,),
    )
    state_conn.commit()

    per_id = {sid: locations.local_audio_path(state_conn, sid) for sid in shapes}
    assert locations.bulk_local_audio_paths(state_conn, list(shapes)) == per_id
    # Control: the fixture reaches both answers, so equality is not vacuous.
    assert per_id["a" * 40] == present and per_id["b" * 40] == via_location
    assert per_id["c" * 40] is None
