"""R2 asset tier: push-then-delete + hydration write-path regressions (ADR 06).

Download hydration pool tests (CLOUDSYNC-09) live in the same file because they
share ``InMemoryAssetS3`` fixtures from ``conftest.py``.

Split out of ``test_asset_tier.py`` (round 4 quality-gate ratchet: that file
crossed 1070 lines, well past the 600-line "too long to hold in your head"
gate). Same contract, same fixtures (``tests/cloudsync/conftest.py``), same
helpers -- imported from ``test_asset_tier`` rather than duplicated so the two
files cannot drift on what a seeded track/policy/pin looks like.

Contract under test: ``specs/design_decision_06.md`` over the v6 policy
tables of ``specs/design_decision_05.md``. Acceptance criteria, one
assertion block each:

- if push-then-delete removes a local file whose upload did not land, the
  one irreversible step in the write path is unguarded -- broken.
- if a hydrated remote row is invisible to the machine that hydrated it, or
  never reaches ``local_changelog``, round 2 finding N1b is back -- broken.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from apps.cloud import asset_store, hydration, hydration_pool, transfer_status
from apps.cloud.config import CloudConfig
from apps.shared.state import locations as state_locations
from apps.shared.state import sync_stamp

from .conftest import InMemoryAssetS3
from .test_asset_tier import (
    _seed_local_location,
    _seed_machine,
    _seed_policy,
    _seed_track,
    _sha,
    _write,
)

pytestmark = pytest.mark.requirement("CAT-04")


# --- push-then-delete ----------------------------------------------------


class _ObservingTransferS3(InMemoryAssetS3):
    """Consume the production progress body while observing its real ledger."""

    def __init__(self, stable_id: str) -> None:
        super().__init__()
        self.stable_id = stable_id
        self.snapshots: list[transfer_status.CloudTransfer] = []

    def put_object_if_none_match(self, bucket: str, key: str, body):
        first = transfer_status.transfer_for(self.stable_id)
        assert first is not None
        self.snapshots.append(first)
        chunks: list[bytes] = []
        while chunk := body.read(4):
            chunks.append(chunk)
            current = transfer_status.transfer_for(self.stable_id)
            assert current is not None
            self.snapshots.append(current)
        return super().put_object_if_none_match(bucket, key, b"".join(chunks))


@pytest.mark.requirement("LIBUX-13")
def test_real_hydration_upload_publishes_byte_progress_and_clears_terminally(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    tmp_path: Path,
) -> None:
    """[if] real upload bytes move [then] TrackTable can read exact progress.

    [if] the production operation returns [then] the in-process entry is gone
    [else stop]: a stale full bar must never masquerade as active transfer.
    """
    stable_id = "t-progress"
    body = b"genuine-upload-bytes"
    s3 = _ObservingTransferS3(stable_id)
    _seed_track(conn, stable_id)
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "pinned", asset_kind="audio")

    hydration.apply_policy_after_produce(
        conn,
        s3,
        cfg,
        stable_id=stable_id,
        machine_id="m1",
        local_path=_write(tmp_path / "out" / "track.flac", body),
        asset_kind="audio",
    )

    assert [(item.direction, item.bytes_total) for item in s3.snapshots] == [
        ("upload", len(body)) for _ in s3.snapshots
    ]
    transferred = [item.bytes_transferred for item in s3.snapshots]
    assert transferred[0] == 0
    assert transferred[-1] == len(body)
    assert transferred == sorted(transferred)
    assert transfer_status.transfer_for(stable_id) is None


class _ObservingDownloadS3(InMemoryAssetS3):
    """Observe download transfer ledger while serving GET bytes."""

    def __init__(self, stable_id: str, body: bytes, bucket: str) -> None:
        super().__init__()
        self.stable_id = stable_id
        self.snapshots: list[transfer_status.CloudTransfer] = []
        digest = hashlib.sha256(body).hexdigest()
        key = asset_store.asset_object_key(digest)
        self.store[(bucket, key)] = (body, self._etag(body))

    def get_object(self, bucket: str, key: str):
        first = transfer_status.transfer_for(self.stable_id)
        assert first is not None
        self.snapshots.append(first)
        result = super().get_object(bucket, key)
        current = transfer_status.transfer_for(self.stable_id)
        assert current is not None
        self.snapshots.append(current)
        return result


@pytest.mark.requirement("CLOUDSYNC-09")
@pytest.mark.requirement("LIBUX-13")
def test_real_hydration_download_publishes_byte_progress_and_clears_terminally(
    cfg: CloudConfig,
    tmp_path: Path,
) -> None:
    """[if] real download bytes move [then] TrackTable can read exact progress.

    [if] the production operation returns [then] the in-process entry is gone
    [else stop]: a stale full bar must never masquerade as active transfer.
    """
    stable_id = "t-download-progress"
    body = b"genuine-download-bytes"
    s3 = _ObservingDownloadS3(stable_id, body, cfg.audio_bucket)
    digest = hashlib.sha256(body).hexdigest()
    dest = tmp_path / "cache" / digest

    hydration.fetch_asset_for_hydration(
        cfg,
        s3,
        digest,
        dest,
        stable_id=stable_id,
        bytes_total=len(body),
    )

    assert [(item.direction, item.bytes_total) for item in s3.snapshots] == [
        ("download", len(body)) for _ in s3.snapshots
    ]
    transferred = [item.bytes_transferred for item in s3.snapshots]
    assert transferred[0] == 0
    assert transferred == sorted(transferred)
    assert transfer_status.transfer_for(stable_id) is None
    assert dest.read_bytes() == body


@pytest.mark.requirement("CLOUDSYNC-09")
def test_concurrent_downloads_respect_pool_cap(
    cfg: CloudConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] cap is 2 [then] a third fetch blocks until a slot frees."""
    monkeypatch.setenv(hydration_pool.ENV_HYDRATION_MAX_CONCURRENT, "2")
    hydration_pool.reset_for_tests()
    body = b"concurrent-download-bytes"
    digest = hashlib.sha256(body).hexdigest()
    key = asset_store.asset_object_key(digest)
    release = threading.Event()
    entered_count = 0
    count_lock = threading.Lock()

    class _SlowDownloadS3(InMemoryAssetS3):
        def get_object(self, bucket: str, key: str):
            nonlocal entered_count
            with count_lock:
                entered_count += 1
            release.wait(timeout=5.0)
            return super().get_object(bucket, key)

    s3 = _SlowDownloadS3()
    s3.store[(cfg.audio_bucket, key)] = (body, s3._etag(body))

    def _fetch(idx: int) -> None:
        asset_store.fetch_asset(cfg, s3, digest, tmp_path / f"dest-{idx}")

    threads = [threading.Thread(target=_fetch, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for _ in range(50):
        if entered_count >= 2:
            break
        time.sleep(0.01)
    assert entered_count == 2
    assert hydration_pool.in_flight() == 2

    release.set()
    for t in threads:
        t.join(timeout=5.0)
    hydration_pool.reset_for_tests()


def test_push_then_delete_keeps_a_pinned_local_file(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    body = b"produced stems"
    produced = _write(tmp_path / "out" / "bundle.zip", body)
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "pinned", asset_kind="stem_bundle")

    outcome = hydration.apply_policy_after_produce(
        conn,
        fake_s3,
        cfg,
        stable_id="t1",
        machine_id="m1",
        local_path=produced,
        asset_kind="stem_bundle",
    )
    assert outcome.local_action == "kept"
    assert outcome.uploaded is True
    assert outcome.local_path == produced
    assert produced.exists()
    assert asset_store.object_exists(cfg, fake_s3, _sha(body)) is True

    row = conn.execute(
        "SELECT remote_url, content_hash, available, origin_device_id "
        "FROM track_locations WHERE stable_id = 't1' AND kind = 'remote'"
    ).fetchone()
    assert row == (outcome.object_key, _sha(body), 1, "m1")


def test_push_then_delete_moves_a_cached_file_into_the_cache(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    body = b"produced stems"
    produced = _write(tmp_path / "out" / "bundle.zip", body)
    cache_dir = tmp_path / "cache"
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "cached", asset_kind="stem_bundle")
    _seed_local_location(conn, "t1", produced)

    outcome = hydration.apply_policy_after_produce(
        conn,
        fake_s3,
        cfg,
        stable_id="t1",
        machine_id="m1",
        local_path=produced,
        asset_kind="stem_bundle",
        cache_dir=cache_dir,
    )
    assert outcome.local_action == "cached"
    assert not produced.exists()
    assert outcome.local_path == hydration.cache_path(cache_dir, _sha(body))
    assert outcome.local_path is not None
    assert outcome.local_path.read_bytes() == body
    available = conn.execute(
        "SELECT available FROM track_locations WHERE stable_id='t1' "
        "AND kind='local'"
    ).fetchone()
    assert available == (0,)


def test_push_then_delete_needs_a_cache_dir_before_it_moves_anything(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    produced = _write(tmp_path / "out" / "bundle.zip", b"produced stems")
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "cached", asset_kind="stem_bundle")
    with pytest.raises(hydration.HydrationError):
        hydration.apply_policy_after_produce(
            conn,
            fake_s3,
            cfg,
            stable_id="t1",
            machine_id="m1",
            local_path=produced,
            asset_kind="stem_bundle",
        )
    assert produced.exists()


@pytest.mark.parametrize("mode", ["stream", "excluded"])
def test_push_then_delete_removes_the_local_copy_under_stream_and_excluded(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
    mode: str,
):
    body = b"produced stems"
    produced = _write(tmp_path / "out" / "bundle.zip", body)
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", mode, asset_kind="stem_bundle")

    outcome = hydration.apply_policy_after_produce(
        conn,
        fake_s3,
        cfg,
        stable_id="t1",
        machine_id="m1",
        local_path=produced,
        asset_kind="stem_bundle",
    )
    assert outcome.local_action == "deleted"
    assert outcome.local_path is None
    assert not produced.exists()
    # Deleted locally ONLY because the bytes are readable back from R2.
    assert asset_store.object_exists(cfg, fake_s3, _sha(body)) is True


@pytest.mark.requirement("LIBUX-13")
def test_a_failed_upload_never_deletes_or_leaves_active_transfer_state(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path
):
    """The single irreversible step in the write path, guarded."""
    s3 = InMemoryAssetS3(fail_puts=True)
    produced = _write(tmp_path / "out" / "bundle.zip", b"produced stems")
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "stream", asset_kind="stem_bundle")

    with pytest.raises((asset_store.AssetStoreError, hydration.HydrationError)):
        hydration.apply_policy_after_produce(
            conn,
            s3,
            cfg,
            stable_id="t1",
            machine_id="m1",
            local_path=produced,
            asset_kind="stem_bundle",
        )
    assert produced.exists()
    assert conn.execute(
        "SELECT COUNT(*) FROM track_locations WHERE kind='remote'"
    ).fetchone() == (0,)
    assert transfer_status.transfer_for("t1") is None


def test_push_then_delete_is_idempotent_for_a_second_producer_run(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    body = b"produced stems"
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "pinned", asset_kind="stem_bundle")

    first = hydration.apply_policy_after_produce(
        conn, fake_s3, cfg,
        stable_id="t1", machine_id="m1",
        local_path=_write(tmp_path / "a" / "bundle.zip", body),
        asset_kind="stem_bundle",
    )
    second = hydration.apply_policy_after_produce(
        conn, fake_s3, cfg,
        stable_id="t1", machine_id="m1",
        local_path=_write(tmp_path / "b" / "bundle.zip", body),
        asset_kind="stem_bundle",
    )
    assert first.object_key == second.object_key
    assert first.uploaded is True
    assert second.uploaded is False
    assert conn.execute(
        "SELECT COUNT(*) FROM track_locations WHERE kind='remote'"
    ).fetchone() == (1,)


def test_push_then_delete_fails_loudly_on_a_missing_produced_file(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "pinned", asset_kind="stem_bundle")
    with pytest.raises(hydration.HydrationError):
        hydration.apply_policy_after_produce(
            conn,
            fake_s3,
            cfg,
            stable_id="t1",
            machine_id="m1",
            local_path=tmp_path / "never-written.zip",
            asset_kind="stem_bundle",
        )


def test_produced_asset_becomes_a_cache_hit_on_the_next_read(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    """End-to-end: write path feeds the read path without a download."""
    body = b"produced stems"
    cache_dir = tmp_path / "cache"
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "cached", asset_kind="audio")

    hydration.apply_policy_after_produce(
        conn,
        fake_s3,
        cfg,
        stable_id="t1",
        machine_id="m1",
        local_path=_write(tmp_path / "out" / "song.flac", body),
        asset_kind="audio",
        cache_dir=cache_dir,
    )
    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=cache_dir, cfg=cfg
    )
    assert source.origin == "cache"
    assert source.content_hash == _sha(body)
    assert source.path is not None and source.path.read_bytes() == body


# --- round 2 finding N1b: hydration must stamp, log and stay in its lane ---


def _changelog(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (str(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT table_name, row_pk, origin_device_id FROM local_changelog "
            "ORDER BY seq"
        )
    ]


def test_a_hydrated_remote_row_is_visible_to_the_machine_that_hydrated_it(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    """Round 2 finding N1b, reproduced as a permanent regression.

    The row used to be written with machine_id NULL, so it was invisible to
    every machine-scoped read:

      [observed] b2 hydrated row: ('9bd3fb3b...', None, 'assets/audio/ab/...')
      [observed] b2 locations.list_locations sees: 0 rows

    A track the daemon had just hydrated from R2 did not register as present
    on the machine that hydrated it, and only the next open_rw backfill
    rescued it -- which a long-lived daemon never performs.
    """
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "pinned", asset_kind="stem_bundle")

    hydration.apply_policy_after_produce(
        conn,
        fake_s3,
        cfg,
        stable_id="t1",
        machine_id="m1",
        local_path=_write(tmp_path / "out" / "bundle.zip", b"produced stems"),
        asset_kind="stem_bundle",
    )

    rows = state_locations.list_locations(conn, "t1", machine_id="m1")
    assert [loc.kind for loc in rows] == ["remote"]
    assert rows[0].machine_id == "m1"
    assert rows[0].available is True
    assert len(rows[0].location_id) == 32, "a uuid4 hex key, not a rowid"


def test_hydration_writes_reach_the_push_fence(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    """[observed] b2 local_changelog entries: 0 -- so the hub never saw it."""
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "cached", asset_kind="stem_bundle")
    produced = _write(tmp_path / "out" / "bundle.zip", b"produced stems")
    _seed_local_location(conn, "t1", produced, machine_id="m1")

    hydration.apply_policy_after_produce(
        conn,
        fake_s3,
        cfg,
        stable_id="t1",
        machine_id="m1",
        local_path=produced,
        asset_kind="stem_bundle",
        cache_dir=tmp_path / "cache",
    )

    entries = _changelog(conn)
    assert entries, (
        "a track_locations write with no local_changelog entry is never "
        "offered to the hub, and the machine then fails its digest compare"
    )
    assert {table for table, _pk, _origin in entries} == {"track_locations"}
    assert {origin for _t, _pk, origin in entries} == {"m1"}

    logged_pks = {pk for _t, pk, _o in entries}
    stored_pks = {
        sync_stamp.encode_row_pk((str(row[0]),))
        for row in conn.execute("SELECT location_id FROM track_locations")
    }
    assert stored_pks - logged_pks == set(), (
        "every location row hydration touched must be named by the changelog"
    )


def test_marking_a_local_copy_gone_does_not_touch_another_machines_row(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    """ADR 08 point 1, running backwards.

    The unscoped UPDATE this replaces flipped every machine's row for the
    same path to unavailable on the strength of a file deleted HERE. Two
    machines sharing a path is the normal case for a fleet on one NAS.
    """
    produced = _write(tmp_path / "out" / "bundle.zip", b"produced stems")
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_machine(conn, "m2")
    _seed_policy(conn, "m1", "stream", asset_kind="stem_bundle")
    _seed_local_location(conn, "t1", produced, machine_id="m1")
    _seed_local_location(conn, "t1", produced, machine_id="m2")

    hydration.apply_policy_after_produce(
        conn,
        fake_s3,
        cfg,
        stable_id="t1",
        machine_id="m1",
        local_path=produced,
        asset_kind="stem_bundle",
    )

    availability = dict(
        conn.execute(
            "SELECT machine_id, available FROM track_locations "
            "WHERE kind = 'local' AND stable_id = 't1'"
        ).fetchall()
    )
    assert availability == {"m1": 0, "m2": 1}, (
        "deleting a file here says nothing about another machine's disk"
    )


def test_re_pushing_the_same_object_reuses_its_location_row(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    """Round 1 finding 1's shape: a second id for one logical row wedges the
    push with a UNIQUE violation the hub can never retry past."""
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "pinned", asset_kind="stem_bundle")
    for _attempt in range(2):
        hydration.apply_policy_after_produce(
            conn,
            fake_s3,
            cfg,
            stable_id="t1",
            machine_id="m1",
            local_path=_write(tmp_path / "out" / "bundle.zip", b"produced stems"),
            asset_kind="stem_bundle",
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM track_locations WHERE kind = 'remote'"
    ).fetchone()[0] == 1
