"""On-demand + bulk R2 stem hydration (ADR-0024).

Uses the SAME asset-tier fake other cloud tests use (``InMemoryAssetS3`` /
``fake_s3`` from ``tests/cloudsync/conftest.py``) and the real strict loader
(:func:`apps.stems.artifacts.load_stem_bundle`) to verify a
hydrated bundle -- never a mocked loader.

* [if] a bundle is already valid on local disk [then] hydration is a no-op
  reported ``already_local``.
* [if] the R2 index has a bundle's manifest + parts [then] hydration writes
  them to disk and the STRICT loader accepts the result.
* [if] a bulk request names a reserved id [then] it is skipped, reason
  ``"reserved"``, UNLESS ``include_reserved=True`` is passed explicitly.
* [if] the SAME id is loaded on-demand (:func:`hydrate_one` with its default
  ``skip_reserved=False``) [then] it is NOT skipped.
* [if] a bundle is open on a deck [then] budget enforcement never evicts
  it even when it is the least-recently-used bundle over budget.
"""
from __future__ import annotations

import errno
import hashlib
import json
import shutil
import subprocess
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import psutil
import pytest

from apps.cloud import asset_store, stem_cache_budget, stem_hydration_locks
from apps.cloud import stem_hydration as stem_hydration_module
from apps.cloud.asset_store import asset_object_key
from apps.cloud.config import CloudConfig
from apps.cloud.stem_cache_budget import GIB, DiskUsage
from apps.cloud.stem_hydration import (
    OPEN_DECK_SERVED_TTL_S,
    OPEN_DECKS,
    HydrationOutcome,
    OpenDeckRegistry,
    bulk_hydrate,
    hydrate_one,
    load_reserved_ids,
)
from apps.cloud.stem_source import (
    STEM_BUNDLE_PRESIGN_FAILED,
    STEM_HUB_AUTH_REFUSED,
    STEM_HUB_INDEX_FAILED,
    STEM_HUB_UNREACHABLE,
    DirectR2Source,
    HubPresignedSource,
    StemSourceError,
    hub_transport_failure_kind,
)
from apps.sync_hub.transport import (
    SyncTransportError,
    is_hub_server_error,
    is_unreachable,
    refused,
)
from apps.stems.artifacts import load_stem_bundle
from tests.cloudsync.conftest import InMemoryAssetS3


def _owner_marker(stable_id: str, pid: int, token: str) -> str:
    """A hydrate-owner marker file name, built the way stem_hydration names it."""
    return f"{stable_id}@{pid}-{token}.json"


def _wav_bytes(*, frames: int = 8, sample_rate: int = 44_100, channels: int = 2) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(b"\x00\x00" * frames * channels)
    return buf.getvalue()


def _manifest_bytes(stable_id: str) -> bytes:
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/music/x.wav", "sha256": "a" * 64},
        "files": {
            "vocals": "vocals.wav",
            "drums": "drums.wav",
            "bass": "bass.wav",
            "other": "other.wav",
        },
    }
    return (json.dumps(manifest) + "\n").encode("utf-8")


def _seed_bundle(fake_s3, cfg: CloudConfig, stable_id: str) -> dict[str, str]:
    """Push a real, strict-loadable bundle's bytes into the fake R2 store and
    return the {filename: sha256} index entry the push rail would journal."""
    from apps.cloud.asset_store import asset_object_key

    files = {
        "manifest.json": _manifest_bytes(stable_id),
        "vocals.wav": _wav_bytes(),
        "drums.wav": _wav_bytes(),
        "bass.wav": _wav_bytes(),
        "other.wav": _wav_bytes(),
    }
    entry: dict[str, str] = {}
    for filename, body in files.items():
        digest = hashlib.sha256(body).hexdigest()
        key = asset_object_key(digest)
        fake_s3.put_object_if_none_match(cfg.audio_bucket, key, body)
        entry[filename] = digest
    return entry


class _TransportFailingAssetS3(InMemoryAssetS3):
    """Raises a bare (non-ClientError) transport exception for one key,
    simulating what a real network failure looks like escaping the
    production boto3 adapter's narrow ClientError handling."""

    def __init__(self, *, fail_key: tuple[str, str]) -> None:
        super().__init__()
        self._fail_key = fail_key

    def get_object(self, bucket, key):
        if (bucket, key) == self._fail_key:
            raise TimeoutError("simulated R2 transport failure")
        return super().get_object(bucket, key)

    def head_object(self, bucket, key):
        if (bucket, key) == self._fail_key:
            raise TimeoutError("simulated R2 transport failure")
        return super().head_object(bucket, key)


def _reservation_file(data_dir: Path, *, reserved_ids: list[str]) -> None:
    path = data_dir / "state" / "stem-order-reserved-100.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"tracks": [{"stable_id": sid} for sid in reserved_ids]}),
        encoding="utf-8",
    )


@pytest.mark.requirement("STEM-12")
def test_hydrate_one_already_local_is_a_noop(tmp_path: Path, fake_s3, cfg: CloudConfig):
    """[if] a bundle exists on disk [then] hydrate_one reports already_local, [else stop]."""
    stems_dir = tmp_path / "stems"
    bundle_dir = stems_dir / "local-track"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "manifest.json").write_bytes(_manifest_bytes("local-track"))
    for part in ("vocals", "drums", "bass", "other"):
        (bundle_dir / f"{part}.wav").write_bytes(_wav_bytes())

    outcome = hydrate_one(
        "local-track",
        data_dir=tmp_path / "data",
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={},
        stems_dir=stems_dir,
    )
    assert outcome.status == "already_local"
    assert fake_s3.head_calls == []  # no network touched


@pytest.mark.requirement("STEM-12")
def test_hydrate_one_fetches_and_strict_loads(tmp_path: Path, fake_s3, cfg: CloudConfig):
    """[if] a bundle is remote-only [then] hydrate_one downloads, loader accepts it, [else stop]."""
    stems_dir = tmp_path / "stems"
    index_entry = _seed_bundle(fake_s3, cfg, "remote-track")

    outcome = hydrate_one(
        "remote-track",
        data_dir=tmp_path / "data",
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={"remote-track": index_entry},
        stems_dir=stems_dir,
    )
    assert outcome.status == "hydrated"
    assert outcome.bytes_fetched > 0
    # The STRICT loader (never mocked) accepts what was written.
    bundle = load_stem_bundle("remote-track", stems_dir=stems_dir)
    assert bundle.manifest.stable_id == "remote-track"
    assert set(bundle.files) == {"vocals", "drums", "bass", "other"}


@pytest.mark.requirement("STEM-12")
def test_hydrate_one_not_in_index_is_unavailable(tmp_path: Path, fake_s3, cfg: CloudConfig):
    """[if] a stable_id has no bundle/index [then] hydrate_one reports unavailable, [else stop]."""
    outcome = hydrate_one(
        "nowhere",
        data_dir=tmp_path / "data",
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={},
        stems_dir=tmp_path / "stems",
    )
    assert outcome.status == "unavailable"


@pytest.mark.requirement("STEM-16")
def test_hydrate_one_indexed_without_manifest_is_error(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    """Indexed but unproducible is an error, not 'unavailable' (the index
    claimed the bundle exists).

    [if] the index has no manifest.json hash [then] hydrate_one errors, [else stop].
    """
    outcome = hydrate_one(
        "headless",
        data_dir=tmp_path / "data",
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={"headless": {"vocals.wav": "a" * 64}},
        stems_dir=tmp_path / "stems",
    )
    assert outcome.status == "error"
    assert outcome.reason is not None
    assert "manifest.json" in outcome.reason


@pytest.mark.requirement("STEM-22")
def test_hydrate_one_rejects_disallowed_index_filename(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    """[if] the index names a traversal filename [then] no writes, hydration errors, [else stop]."""
    stems_dir = tmp_path / "stems"
    stable_id = "escape-track"
    index_entry = _seed_bundle(fake_s3, cfg, stable_id)
    del index_entry["vocals.wav"]
    index_entry["../escape.wav"] = "f" * 64

    parent_dir = stems_dir.parent
    outcome = hydrate_one(
        stable_id,
        data_dir=tmp_path / "data",
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={stable_id: index_entry},
        stems_dir=stems_dir,
    )
    assert outcome.status == "error"
    assert "../escape.wav" in (outcome.reason or "")
    assert not (stems_dir / stable_id).exists()
    # The filename check runs before any bundle write, so the whole stems_dir
    # tree (not just the bundle) must never have been created, and no file is
    # left anywhere. The one directory a hydrate makes first is its owner-marker
    # directory under (private path) (the marker itself is removed on return).
    assert not stems_dir.exists()
    assert [p for p in parent_dir.rglob("*") if not p.is_dir()] == []
    assert sorted(p.relative_to(parent_dir).as_posix() for p in parent_dir.rglob("*")) == [
        "data",
        "(private path)",
        "(private path)",
    ]


@pytest.mark.requirement("STEM-12")
def test_a_generation_write_that_raises_os_error_still_keeps_the_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] a raw OSError hits the closing generation write [then] the publish stands, [else stop].

    A publish already renamed and verified must not be lost to a raw I/O
    failure in the generation file. MUTATION TARGET: drop the OSError
    conversion and the closing write raises out of the window.
    """
    writes = {"n": 0}
    real_write_text = Path.write_text

    def fail_the_closing_write(self: Path, data: str, *args, **kwargs) -> int:
        if self.name.endswith(".gen.tmp"):
            writes["n"] += 1
            if writes["n"] == 2:
                raise OSError(errno.ENOSPC, "no space left on device")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_the_closing_write)
    with stem_hydration_module.bundle_publish_window(tmp_path / "data", "track"):
        pass

    assert writes["n"] == 2
    assert stem_hydration_module.read_hydrate_generation(tmp_path / "data", "track") % 2 == 1


@pytest.mark.requirement("STEM-12")
def test_hydrate_one_returns_an_error_when_its_owner_marker_cannot_be_published(
    tmp_path: Path, fake_s3, cfg: CloudConfig, monkeypatch: pytest.MonkeyPatch
):
    """[if] the owner marker cannot be published [then] the named error outcome returns, [else stop].

    The marker is written before any lock or bundle read, so a failure there
    must come back as an ``error`` outcome, not an exception out of the call.
    MUTATION TARGET: let the marker error escape and this test raises.
    """
    stems_dir = tmp_path / "stems"
    stable_id = "marker-blocked-track"
    index_entry = _seed_bundle(fake_s3, cfg, stable_id)

    def refuse_replace(src, dst):
        raise PermissionError("marker target is held open")

    monkeypatch.setattr(stem_hydration_locks.os, "replace", refuse_replace)
    outcome = hydrate_one(
        stable_id,
        data_dir=tmp_path / "data",
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={stable_id: index_entry},
        stems_dir=stems_dir,
    )
    assert outcome.status == "error"
    assert "could not write" in (outcome.reason or "")
    assert not stems_dir.exists()


@pytest.mark.requirement("STEM-12")
def test_hydrate_one_leaves_no_partial_bundle_on_fetch_failure(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    """A part's hash is in the index but not actually in the fake store:
    the fetch must fail, and the half-written directory must not remain.

    [if] a fetch fails partway [then] hydrate_one errors, no partial bundle dir, [else stop].
    """
    stems_dir = tmp_path / "stems"
    index_entry = _seed_bundle(fake_s3, cfg, "broken-track")
    index_entry["vocals.wav"] = "f" * 64  # never pushed -> fetch_asset raises

    outcome = hydrate_one(
        "broken-track",
        data_dir=tmp_path / "data",
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={"broken-track": index_entry},
        stems_dir=stems_dir,
    )
    assert outcome.status == "error"
    assert not (stems_dir / "broken-track").exists()


@pytest.mark.requirement("STEM-58")
def test_a_failed_hydrate_that_cannot_remove_its_copy_says_so(
    tmp_path: Path, fake_s3, cfg: CloudConfig, monkeypatch: pytest.MonkeyPatch
):
    """[if] a fetch fails and its temp dir cannot be removed [then] the error names that dir, [else stop].

    A ``.tmp-hydrate-*`` directory is outside every cache scan, so one left
    behind unseen would never be counted or evicted (Codex on #6821). The only
    double is the removal refusing, which stands in for an I/O error or a
    Windows sharing violation.
    """
    stems_dir = tmp_path / "stems"
    index_entry = _seed_bundle(fake_s3, cfg, "stuck-track")
    index_entry["vocals.wav"] = "f" * 64  # never pushed -> fetch_asset raises
    real_rmtree = shutil.rmtree

    def refuse_temp_dirs(path, ignore_errors=False, **kwargs):
        if ".tmp-hydrate-" in str(path):
            if ignore_errors:
                return None
            raise PermissionError(13, "sharing violation", str(path))
        return real_rmtree(path, ignore_errors=ignore_errors, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", refuse_temp_dirs)

    outcome = hydrate_one(
        "stuck-track",
        data_dir=tmp_path / "data",
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={"stuck-track": index_entry},
        stems_dir=stems_dir,
    )

    assert outcome.status == "error"
    left = [p for p in stems_dir.parent.rglob("*") if ".tmp-hydrate-" in p.name]
    assert left, "control: the refused removal left the temp dir"
    assert "could not remove failed hydration copy" in (outcome.reason or "")
    assert str(left[0]) in (outcome.reason or ""), outcome.reason
    assert not (stems_dir / "stuck-track").exists()


# --- reservation guard: bulk skips, on-demand does not (D5) ------------------


@pytest.mark.requirement("STEM-13")
def test_bulk_hydrate_skips_reserved_by_default(tmp_path: Path, fake_s3, cfg: CloudConfig):
    """[if] reserved, include_reserved unset [then] bulk_hydrate skips it, [else stop]."""
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    entry = _seed_bundle(fake_s3, cfg, "reserved-track")
    _reservation_file(data_dir, reserved_ids=["reserved-track"])

    report = bulk_hydrate(
        ["reserved-track"],
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={"reserved-track": entry},
        byte_budget=10**9,
        stems_dir=stems_dir,
    )
    assert len(report.fetched) == 0
    assert report.skipped[0].status == "reserved_skip"
    assert report.skipped[0].reason == "reserved"
    assert not (stems_dir / "reserved-track").exists()


@pytest.mark.requirement("STEM-13")
def test_bulk_hydrate_includes_reserved_only_with_explicit_flag(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    """[if] include_reserved=True [then] bulk_hydrate fetches the reserved track, [else stop]."""
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    entry = _seed_bundle(fake_s3, cfg, "reserved-track")
    _reservation_file(data_dir, reserved_ids=["reserved-track"])

    report = bulk_hydrate(
        ["reserved-track"],
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={"reserved-track": entry},
        byte_budget=10**9,
        include_reserved=True,
        stems_dir=stems_dir,
    )
    assert len(report.fetched) == 1
    assert report.fetched[0].status == "hydrated"


@pytest.mark.requirement("STEM-13")
def test_on_demand_hydrate_one_never_skips_reserved(tmp_path: Path, fake_s3, cfg: CloudConfig):
    """MUTATION TARGET: flip skip_reserved=True here and this test goes red
    -- the deck-load path must be able to hydrate a reserved id.

    [if] a deck-load hydrates a reserved track, skip_reserved=False [then] it hydrates, [else stop].
    """
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    entry = _seed_bundle(fake_s3, cfg, "reserved-track")
    _reservation_file(data_dir, reserved_ids=["reserved-track"])

    outcome = hydrate_one(
        "reserved-track",
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={"reserved-track": entry},
        stems_dir=stems_dir,
        skip_reserved=False,  # the on-demand deck-load caller's contract
    )
    assert outcome.status == "hydrated"


@pytest.mark.requirement("STEM-13")
def test_load_reserved_ids_missing_file_is_empty(tmp_path: Path):
    """[if] no reservation file exists [then] load_reserved_ids returns empty, [else stop]."""
    assert load_reserved_ids(tmp_path / "data") == frozenset()


@pytest.mark.requirement("STEM-13")
def test_load_reserved_ids_reads_tracks_array(tmp_path: Path):
    """[if] a reservation file lists tracks [then] load_reserved_ids matches it, [else stop]."""
    data_dir = tmp_path / "data"
    _reservation_file(data_dir, reserved_ids=["a", "b"])
    assert load_reserved_ids(data_dir) == frozenset({"a", "b"})


# --- budget-bounded LRU eviction, open-deck protected -------------------------


#: A volume with nothing free: every R2-confirmed, unprotected bundle is over
#: budget, so these tests exercise WHICH bundle goes, not whether one does.
_FULL_DISK = DiskUsage(total_bytes=460 * GIB, free_bytes=0)


def _make_bundle_on_disk(stems_dir: Path, stable_id: str, *, atime: float) -> dict[str, str]:
    """Write a bundle and return its {filename: sha256} index entry, so a test
    can declare it R2-confirmed by passing the entry in the index."""
    import os

    bundle_dir = stems_dir / stable_id
    bundle_dir.mkdir(parents=True)
    bodies = {"manifest.json": _manifest_bytes(stable_id)}
    for part in ("vocals", "drums", "bass", "other"):
        bodies[f"{part}.wav"] = _wav_bytes()
    entry: dict[str, str] = {}
    for filename, body in bodies.items():
        path = bundle_dir / filename
        path.write_bytes(body)
        os.utime(path, (atime, atime))
        entry[filename] = hashlib.sha256(body).hexdigest()
    return entry


def _enforce_on_full_disk(stems_dir: Path, index, *, protected=frozenset()):
    return stem_cache_budget.enforce(
        stems_dir,
        data_dir=stems_dir.parent / "data",
        index=index,
        protected=protected,
        can_rehydrate=True,
        disk=_FULL_DISK,
    )


@pytest.mark.requirement("STEM-14")
def test_enforcement_evicts_least_recently_used_bundle_first(tmp_path: Path):
    """[if] the cache is over budget by one bundle [then] the LRU one goes, [else stop]."""
    stems_dir = tmp_path / "stems"
    index = {
        "old": _make_bundle_on_disk(stems_dir, "old", atime=1_000_000),
        "new": _make_bundle_on_disk(stems_dir, "new", atime=2_000_000),
    }
    size_per_bundle = sum(f.stat().st_size for f in (stems_dir / "old").iterdir())
    floor = stem_cache_budget.floor_bytes(
        _FULL_DISK.total_bytes, stem_cache_budget.StemCacheSettings()
    )
    outcome = stem_cache_budget.enforce(
        stems_dir,
        data_dir=tmp_path / "data",
        index=index,
        protected=frozenset(),
        can_rehydrate=True,
        disk=DiskUsage(total_bytes=_FULL_DISK.total_bytes, free_bytes=floor - size_per_bundle),
    )
    assert outcome.evicted_stable_ids == ("old",)
    assert (stems_dir / "new").exists()


@pytest.mark.requirement("STEM-14")
def test_enforcement_never_evicts_an_open_deck(tmp_path: Path):
    """MUTATION TARGET: pass ``protected=frozenset()`` instead of the real
    open-deck set here and this test goes red.

    [if] the oldest bundle belongs to an open deck [then] it is never evicted, [else stop].
    """
    stems_dir = tmp_path / "stems"
    index = {
        "oldest-but-open": _make_bundle_on_disk(stems_dir, "oldest-but-open", atime=1_000_000),
        "newer": _make_bundle_on_disk(stems_dir, "newer", atime=2_000_000),
    }
    registry = OpenDeckRegistry()
    registry.mark_open("oldest-but-open")
    outcome = _enforce_on_full_disk(stems_dir, index, protected=registry.open_ids())
    assert "oldest-but-open" not in outcome.evicted_stable_ids
    assert (stems_dir / "oldest-but-open").exists()
    assert outcome.evicted_stable_ids == ("newer",)


@pytest.mark.requirement("STEM-21")
def test_bulk_hydrate_skips_oversized_bundle_before_download(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    """A bundle larger than the remaining byte budget must never start downloading.

    [if] a bundle exceeds the byte budget [then] bulk_hydrate skips downloading it, [else stop].
    """
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    entry = _seed_bundle(fake_s3, cfg, "big-track")

    report = bulk_hydrate(
        ["big-track"],
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={"big-track": entry},
        byte_budget=10,
        stems_dir=stems_dir,
    )
    assert len(report.fetched) == 0
    assert report.skipped[0].status == "unavailable"
    assert report.skipped[0].reason is not None
    assert "byte_budget" in report.skipped[0].reason
    assert not (stems_dir / "big-track").exists()


class _FailOnSecondPresignSource:
    """Hub source that fails presign on the second bundle's size probe."""

    mode = "hub_presigned"

    def __init__(self, cfg: CloudConfig, s3: InMemoryAssetS3) -> None:
        self._direct = DirectR2Source(cfg=cfg, s3=s3)
        self._presign_calls = 0

    def refresh_index(self, data_dir: Path, *, force: bool = False) -> bool:
        return False

    def refresh_error(self, data_dir: Path) -> str | None:
        return None

    def bundle_remote_size(
        self, file_hashes: dict[str, str], *, stable_id: str | None = None
    ) -> int | None:
        self._presign_calls += 1
        if self._presign_calls >= 2:
            raise StemSourceError(
                STEM_HUB_INDEX_FAILED,
                "bundle presign: hub answered HTTP 503 (down)",
            )
        return self._direct.bundle_remote_size(file_hashes, stable_id=stable_id)

    def fetch_bundle_files(
        self,
        *,
        stable_id: str,
        file_hashes: dict[str, str],
        tmp_dir: Path,
    ) -> int:
        return self._direct.fetch_bundle_files(
            stable_id=stable_id,
            file_hashes=file_hashes,
            tmp_dir=tmp_dir,
        )


class _FailOnFirstPresignSource:
    """Hub source that fails the first bundle_remote_size probe (zero progress)."""

    mode = "hub_presigned"

    def __init__(self, cfg: CloudConfig, s3: InMemoryAssetS3) -> None:
        self._direct = DirectR2Source(cfg=cfg, s3=s3)

    def refresh_index(self, data_dir: Path, *, force: bool = False) -> bool:
        return False

    def refresh_error(self, data_dir: Path) -> str | None:
        return None

    def bundle_remote_size(
        self, file_hashes: dict[str, str], *, stable_id: str | None = None
    ) -> int | None:
        raise StemSourceError(
            STEM_HUB_UNREACHABLE,
            "bundle presign: connection refused",
        )

    def fetch_bundle_files(
        self,
        *,
        stable_id: str,
        file_hashes: dict[str, str],
        tmp_dir: Path,
    ) -> int:
        return self._direct.fetch_bundle_files(
            stable_id=stable_id,
            file_hashes=file_hashes,
            tmp_dir=tmp_dir,
        )


_SYNCEXPORT_PARTIAL_BULK = "SYNCEXPORT-14747"


@pytest.mark.requirement(_SYNCEXPORT_PARTIAL_BULK)
def test_bulk_hydrate_preserves_partial_fetched_on_mid_pass_hub_failure(
    tmp_path: Path, fake_s3, cfg: CloudConfig
) -> None:
    """[if] first id hydrates then hub fails [then] fetched kept, rest hub_error, [else stop]."""
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    stable_ids = ["first-track", "second-track", "third-track"]
    index = {stable_id: _seed_bundle(fake_s3, cfg, stable_id) for stable_id in stable_ids}
    source = _FailOnSecondPresignSource(cfg, fake_s3)

    report = bulk_hydrate(
        stable_ids,
        data_dir=data_dir,
        source=source,
        index=index,
        byte_budget=10**9,
        stems_dir=stems_dir,
    )
    assert report.bytes_fetched > 0
    assert report.fetched[0].stable_id == "first-track"
    assert (stems_dir / "first-track" / "manifest.json").is_file()
    assert {row.stable_id for row in report.skipped} == {"second-track", "third-track"}
    assert all(row.status == "hub_error" for row in report.skipped)


@pytest.mark.requirement(_SYNCEXPORT_PARTIAL_BULK)
def test_bulk_hydrate_reraises_hub_error_when_no_progress(
    tmp_path: Path, fake_s3, cfg: CloudConfig
) -> None:
    """[if] hub fails before any bulk progress [then] bulk_hydrate re-raises, [else stop]."""
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    stable_ids = ["only-track", "also-track"]
    index = {sid: _seed_bundle(fake_s3, cfg, sid) for sid in stable_ids}
    source = _FailOnFirstPresignSource(cfg, fake_s3)

    with pytest.raises(StemSourceError, match="connection refused") as exc_info:
        bulk_hydrate(
            stable_ids,
            data_dir=data_dir,
            source=source,
            index=index,
            byte_budget=10**9,
            stems_dir=stems_dir,
        )
    assert exc_info.value.code == STEM_HUB_UNREACHABLE
    assert not any((stems_dir / sid).exists() for sid in stable_ids)


@pytest.mark.requirement(_SYNCEXPORT_PARTIAL_BULK)
def test_bulk_hydrate_all_requested_ids_succeed(
    tmp_path: Path, fake_s3, cfg: CloudConfig
) -> None:
    """[if] every bulk id can hydrate [then] all land in fetched, none hub_error, [else stop]."""
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    stable_ids = ["bulk-a", "bulk-b", "bulk-c"]
    index = {sid: _seed_bundle(fake_s3, cfg, sid) for sid in stable_ids}

    report = bulk_hydrate(
        stable_ids,
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index=index,
        byte_budget=10**9,
        stems_dir=stems_dir,
    )
    assert len(report.fetched) == 3
    assert {o.stable_id for o in report.fetched} == set(stable_ids)
    assert all(o.status in ("hydrated", "already_local") for o in report.fetched)
    assert report.bytes_fetched > 0
    assert all((stems_dir / sid / "manifest.json").is_file() for sid in stable_ids)
    assert not any(o.status == "hub_error" for o in report.skipped)


@pytest.mark.requirement(_SYNCEXPORT_PARTIAL_BULK)
def test_bulk_hydrate_retains_prior_fetch_when_later_id_digest_fails(
    tmp_path: Path, fake_s3, cfg: CloudConfig
) -> None:
    """[if] a later bulk id fails verify [then] earlier fetches remain on disk, [else stop]."""
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    good_id, bad_id = "good-track", "bad-track"
    good_entry = _seed_bundle(fake_s3, cfg, good_id)
    bad_entry = _seed_bundle(fake_s3, cfg, bad_id)
    bad_entry["vocals.wav"] = "f" * 64

    report = bulk_hydrate(
        [good_id, bad_id],
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={good_id: good_entry, bad_id: bad_entry},
        byte_budget=10**9,
        stems_dir=stems_dir,
    )
    assert any(o.stable_id == good_id and o.status == "hydrated" for o in report.fetched)
    assert (stems_dir / good_id / "manifest.json").is_file()
    bad_rows = [o for o in report.skipped if o.stable_id == bad_id]
    assert len(bad_rows) == 1
    assert bad_rows[0].status == "error"
    assert not (stems_dir / bad_id).exists()


@pytest.mark.requirement("STEM-35")
def test_bulk_hydrate_marks_remainder_hub_error_after_hub_5xx(
    tmp_path: Path, fake_s3, cfg: CloudConfig
) -> None:
    """[if] hub 5xx mid bulk pass [then] remainder rows are hub_error, [else stop]."""
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    stable_ids = ["first-track", "second-track", "third-track"]
    index = {stable_id: _seed_bundle(fake_s3, cfg, stable_id) for stable_id in stable_ids}
    source = _FailOnSecondPresignSource(cfg, fake_s3)

    report = bulk_hydrate(
        stable_ids,
        data_dir=data_dir,
        source=source,
        index=index,
        byte_budget=10**9,
        stems_dir=stems_dir,
    )
    assert report.bytes_fetched > 0
    assert report.fetched[0].stable_id == "first-track"
    assert {row.stable_id for row in report.skipped} == {"second-track", "third-track"}
    assert all(row.status == "hub_error" for row in report.skipped)


@pytest.mark.requirement("STEM-21")
def test_bulk_hydrate_fits_bundle_within_budget_still_hydrates(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    """HEAD-based budget gating must not block a bundle that legitimately fits.

    [if] a bundle fits the byte budget [then] bulk_hydrate still hydrates it, [else stop].
    """
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    entry = _seed_bundle(fake_s3, cfg, "fits-track")

    report = bulk_hydrate(
        ["fits-track"],
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={"fits-track": entry},
        byte_budget=10**9,
        stems_dir=stems_dir,
    )
    assert len(report.fetched) == 1
    assert report.fetched[0].status == "hydrated"
    assert (stems_dir / "fits-track" / "manifest.json").exists()


@pytest.mark.requirement("STEM-14")
def test_open_deck_registry_is_refcounted():
    """[if] a deck opens twice [then] one close leaves it open, a second closes it, [else stop]."""
    registry = OpenDeckRegistry()
    registry.mark_open("t")
    registry.mark_open("t")
    registry.mark_closed("t")
    assert registry.is_open("t")  # one reference remains
    registry.mark_closed("t")
    assert not registry.is_open("t")


# --- transport failure classification (STEM-27) ------------------------------


@pytest.mark.requirement("STEM-27")
def test_hydrate_one_classifies_transport_failure_as_error(
    tmp_path: Path, cfg: CloudConfig
):
    """[if] R2 fails mid-fetch [then] hydrate_one errors naming it, not unavailable, [else stop]."""
    stems_dir = tmp_path / "stems"
    stable_id = "transport-track"
    base_s3 = InMemoryAssetS3()
    entry = _seed_bundle(base_s3, cfg, stable_id)
    fail_key = (cfg.audio_bucket, asset_object_key(entry["vocals.wav"]))
    s3 = _TransportFailingAssetS3(fail_key=fail_key)
    for bucket, key in base_s3.store:
        s3.store[(bucket, key)] = base_s3.store[(bucket, key)]

    outcome = hydrate_one(
        stable_id,
        data_dir=tmp_path / "data",
        source=DirectR2Source(cfg=cfg, s3=s3),
        index={stable_id: entry},
        stems_dir=stems_dir,
    )
    assert outcome.status == "error"
    assert "simulated R2 transport failure" in (outcome.reason or "")
    assert not (stems_dir / stable_id).exists()


@pytest.mark.requirement("STEM-27")
def test_bundle_remote_size_classifies_head_transport_failure(cfg: CloudConfig):
    """[if] the HEAD request fails [then] _bundle_remote_size raises, [else stop]."""
    stable_id = "head-fail-track"
    base_s3 = InMemoryAssetS3()
    entry = _seed_bundle(base_s3, cfg, stable_id)
    fail_key = (cfg.audio_bucket, asset_object_key(entry["vocals.wav"]))
    s3 = _TransportFailingAssetS3(fail_key=fail_key)
    for bucket, key in base_s3.store:
        s3.store[(bucket, key)] = base_s3.store[(bucket, key)]

    source = DirectR2Source(cfg=cfg, s3=s3)
    with pytest.raises(asset_store.AssetStoreError, match="simulated R2 transport failure"):
        source.bundle_remote_size(entry, stable_id=stable_id)


# --- hub transport failure classification seam (STEM-35) ---------------------


class _ScriptedHubTransport:
    """Hub transport whose every GET raises one scripted error or returns one body."""

    def __init__(
        self,
        *,
        error: SyncTransportError | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        self._error = error
        self._body = body

    def get(
        self, path: str, params: Mapping[str, str | Sequence[str]]
    ) -> dict[str, Any]:
        """Raise the scripted error, else answer the scripted body."""
        if self._error is not None:
            raise self._error
        assert self._body is not None
        return self._body

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Refuse every POST: the classifier seam only exercises GETs."""
        raise SyncTransportError(f"unexpected POST {path}")


def _scripted_hub_source(
    tmp_path: Path, transport: _ScriptedHubTransport
) -> HubPresignedSource:
    """Build a hub-presigned source wired to a scripted transport."""
    return HubPresignedSource(
        data_dir=tmp_path,
        hub_url="http://hub.invalid:8787",
        machine_id="spoke-test",
        bearer=None,
        transport=transport,
    )


def _size_probe_error(
    tmp_path: Path, error: SyncTransportError
) -> StemSourceError:
    """Return the StemSourceError the hub size probe raises for one transport error."""
    source = _scripted_hub_source(tmp_path, _ScriptedHubTransport(error=error))
    with pytest.raises(StemSourceError) as excinfo:
        source.bundle_remote_size({}, stable_id="classified-track")
    return excinfo.value


@pytest.mark.requirement("STEM-35")
def test_transport_classifiers_split_unreachable_from_hub_answers() -> None:
    """[if] transport classifiers blur unreachable, 5xx and 401 [then] broken, [else stop]."""
    unreachable = SyncTransportError("GET /x failed: Connection refused")
    server_error = refused("GET /x", 502, '{"detail":{"code":"HUB_DOWN"}}')
    auth_refused = refused("GET /x", 401, '{"detail":{"code":"SYNC_AUTH"}}')

    assert is_unreachable(unreachable) is True
    assert is_hub_server_error(unreachable) is False
    assert is_unreachable(server_error) is False
    assert is_hub_server_error(server_error) is True
    assert is_unreachable(auth_refused) is False
    assert is_hub_server_error(auth_refused) is False


@pytest.mark.requirement("STEM-35")
def test_hub_connection_failure_classifies_as_unreachable(tmp_path: Path) -> None:
    """[if] a hub connection cannot be established [then] the failure kind is unreachable, [else stop]."""
    exc = _size_probe_error(
        tmp_path, SyncTransportError("GET /x failed: Connection refused")
    )
    assert exc.code == STEM_HUB_UNREACHABLE
    assert hub_transport_failure_kind(exc) == "unreachable"


@pytest.mark.requirement("STEM-35")
def test_hub_5xx_classifies_as_hub_5xx_not_unreachable(tmp_path: Path) -> None:
    """[if] a hub returns a 5xx response [then] the failure kind is hub_5xx, [else stop]."""
    exc = _size_probe_error(
        tmp_path, refused("GET /x", 503, '{"detail":{"code":"HUB_DOWN"}}')
    )
    assert exc.code == STEM_HUB_INDEX_FAILED
    assert "HTTP 503" in exc.message
    assert hub_transport_failure_kind(exc) == "hub_5xx"


@pytest.mark.requirement("STEM-35")
def test_hub_auth_refusal_is_not_a_transport_failure(tmp_path: Path) -> None:
    """[if] a hub rejects credentials [then] it is auth refused, not a transport kind, [else stop]."""
    exc = _size_probe_error(
        tmp_path, refused("GET /x", 401, '{"detail":{"code":"SYNC_AUTH"}}')
    )
    assert exc.code == STEM_HUB_AUTH_REFUSED
    assert hub_transport_failure_kind(exc) is None


@pytest.mark.requirement("STEM-35")
def test_local_bundle_validation_error_is_not_a_transport_failure(
    tmp_path: Path,
) -> None:
    """[if] a local bundle validation error occurs [then] no transport kind is assigned, [else stop]."""
    presign = {
        "files": [
            {
                "filename": "vocals.wav",
                "content_hash": "b" * 64,
                "size_bytes": 8,
                "url": "http://hub.invalid/vocals.wav",
            }
        ]
    }
    source = _scripted_hub_source(tmp_path, _ScriptedHubTransport(body=presign))
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    with pytest.raises(StemSourceError) as excinfo:
        source.fetch_bundle_files(
            stable_id="classified-track",
            file_hashes={"vocals.wav": "a" * 64},
            tmp_dir=tmp_dir,
        )
    assert excinfo.value.code == STEM_BUNDLE_PRESIGN_FAILED
    assert "does not match local index" in excinfo.value.message
    assert hub_transport_failure_kind(excinfo.value) is None


# --- concurrent hydration safety (STEM-28) -------------------------------------


@pytest.mark.requirement("STEM-28")
def test_hydrate_one_concurrent_calls_leave_exactly_one_bundle(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    """[if] 4 threads hydrate one id at once [then] one bundle lands, no temp dirs, [else stop]"""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    stable_id = "concurrent-track"
    entry = _seed_bundle(fake_s3, cfg, stable_id)
    index = {stable_id: entry}

    def _run() -> HydrationOutcome:
        return hydrate_one(
            stable_id,
            data_dir=data_dir,
            source=DirectR2Source(cfg=cfg, s3=fake_s3),
            index=index,
            stems_dir=stems_dir,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_run) for _ in range(4)]
        outcomes = [f.result() for f in as_completed(futures)]

    assert all(o.status in ("hydrated", "already_local") for o in outcomes)
    load_stem_bundle(stable_id, stems_dir=stems_dir)
    assert list(stems_dir.glob(f"{stable_id}.tmp-hydrate-*")) == []


@pytest.mark.requirement("STEM-28")
def test_hydrate_one_failure_removes_only_its_own_temp_dir(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    """[if] a hydration fails after another succeeds [then] only its dir removed, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    good_id = "good-track"
    bad_id = "broken-track"
    good_entry = _seed_bundle(fake_s3, cfg, good_id)
    bad_entry = _seed_bundle(fake_s3, cfg, bad_id)
    bad_entry["vocals.wav"] = "f" * 64

    good_outcome = hydrate_one(
        good_id,
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={good_id: good_entry},
        stems_dir=stems_dir,
    )
    assert good_outcome.status == "hydrated"
    good_manifest = stems_dir / good_id / "manifest.json"
    assert good_manifest.is_file()
    good_mtime = good_manifest.stat().st_mtime

    bad_outcome = hydrate_one(
        bad_id,
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={bad_id: bad_entry},
        stems_dir=stems_dir,
    )
    assert bad_outcome.status == "error"
    assert good_manifest.is_file()
    assert good_manifest.stat().st_mtime == good_mtime
    assert not (stems_dir / bad_id).exists()


# --- just-hydrated self-protection (STEM-29) ---------------------------------


@pytest.mark.requirement("STEM-29")
def test_hydrate_one_protects_its_own_just_hydrated_bundle(
    tmp_path: Path, fake_s3, cfg: CloudConfig, monkeypatch: pytest.MonkeyPatch
):
    """[if] enforcement runs right after a hydrate on a full disk [then] the bundle still lands, and so does a bundle open on a deck, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(stem_cache_budget, "measure_disk", lambda _path: _FULL_DISK)

    open_entry = _make_bundle_on_disk(stems_dir, "open-track", atime=1_000_000)
    OPEN_DECKS.mark_open("open-track")
    try:
        entry = _seed_bundle(fake_s3, cfg, "new-track")
        outcome = hydrate_one(
            "new-track",
            data_dir=data_dir,
            source=DirectR2Source(cfg=cfg, s3=fake_s3),
            index={"new-track": entry, "open-track": open_entry},
            stems_dir=stems_dir,
        )
        assert outcome.status == "hydrated"
        assert (stems_dir / "new-track" / "manifest.json").exists()
        assert (stems_dir / "open-track" / "manifest.json").exists()
    finally:
        OPEN_DECKS.mark_closed("open-track")


# --- serve-TTL eviction protection (STEM-30) ---------------------------------


@pytest.mark.requirement("STEM-30")
def test_open_deck_registry_served_ttl_protects_then_expires():
    """[if] a deck is served at time 0 [then] it stays open until the TTL expires, [else stop]."""
    registry = OpenDeckRegistry()
    registry.mark_served("t", now=0.0)
    assert "t" in registry.open_ids(now=0.0)
    assert "t" in registry.open_ids(now=OPEN_DECK_SERVED_TTL_S - 1)
    assert "t" not in registry.open_ids(now=OPEN_DECK_SERVED_TTL_S + 1)


@pytest.mark.requirement("STEM-30")
def test_enforcement_protects_a_recently_served_bundle_without_deck_open(tmp_path: Path):
    """[if] a bundle was recently served, no open deck [then] protected until TTL, [else stop]."""
    stems_dir = tmp_path / "stems"
    index = {
        "served-track": _make_bundle_on_disk(stems_dir, "served-track", atime=1_000_000),
        "newer": _make_bundle_on_disk(stems_dir, "newer", atime=2_000_000),
    }
    registry = OpenDeckRegistry()
    served_at = 100.0
    registry.mark_served("served-track", now=served_at)

    protected = registry.open_ids(now=served_at)
    outcome = _enforce_on_full_disk(stems_dir, index, protected=protected)
    assert "served-track" not in outcome.evicted_stable_ids
    assert (stems_dir / "served-track").exists()

    expired_protected = registry.open_ids(now=served_at + OPEN_DECK_SERVED_TTL_S + 1)
    outcome2 = _enforce_on_full_disk(stems_dir, index, protected=expired_protected)
    assert "served-track" in outcome2.evicted_stable_ids


@pytest.mark.requirement("STEM-12")
@pytest.mark.parametrize(
    ("bad_create_time", "bad_started_at"),
    [
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        (1.0, float("-inf")),
        (10**400, 1.0),
    ],
)
def test_an_owner_marker_with_non_finite_timestamps_is_removed(
    tmp_path: Path, bad_create_time: float | int, bad_started_at: float
):
    """[if] create_time or started_at is non-finite [then] marker removed, not fetching, [else stop].

    JSON may decode NaN/Infinity; huge ints overflow float() -- all must unlink, never stall
    in :func:`hydrate_owner_alive`. MUTATION TARGET: drop :func:`_is_finite_stamp` and NaN survives.
    """
    data_dir = tmp_path / "data"
    owners = stem_hydration_module.hydrate_owners_dir(data_dir)
    owners.mkdir(parents=True)
    marker = owners / _owner_marker("shape-track", 4242, "bad-stamp")
    marker.write_text(
        json.dumps(
            {"pid": 4242, "create_time": bad_create_time, "started_at": bad_started_at},
            allow_nan=True,
        ),
        encoding="utf-8",
        newline="\n",
    )

    assert stem_hydration_module.hydrate_owner_alive(data_dir, "shape-track") is False
    assert not marker.exists()


@pytest.mark.requirement("STEM-12")
def test_an_owner_marker_with_the_wrong_json_shape_is_removed_not_raised(tmp_path: Path):
    """[if] an owner marker has the wrong JSON shape [then] it is removed and ignored, [else stop].

    Valid JSON that is not an owner record (a list, or no pid) must not raise
    out of the owner check and turn the state endpoint into a 500.
    MUTATION TARGET: drop the shape check and this test raises.
    """
    data_dir = tmp_path / "data"
    owners = stem_hydration_module.hydrate_owners_dir(data_dir)
    owners.mkdir(parents=True)
    list_marker = owners / _owner_marker("shape-track", 1, "deadbeef")
    list_marker.write_text("[]", encoding="utf-8", newline="\n")
    no_pid_marker = owners / _owner_marker("shape-track", 2, "cafef00d")
    no_pid_marker.write_text(json.dumps({"create_time": 1.0}), encoding="utf-8", newline="\n")
    bad_pid_marker = owners / _owner_marker("shape-track", 3, "0badf00d")
    bad_pid_marker.write_text(
        json.dumps({"pid": -1, "create_time": 0.0, "started_at": time.time()}),
        encoding="utf-8",
        newline="\n",
    )

    assert stem_hydration_module.hydrate_owner_alive(data_dir, "shape-track") is False
    assert not list_marker.exists()
    assert not no_pid_marker.exists()
    assert not bad_pid_marker.exists()


@pytest.mark.requirement("STEM-12")
def test_an_owner_marker_past_the_ttl_is_removed_only_when_its_owner_is_dead(tmp_path: Path):
    """[if] a marker is past the hydrate TTL [then] removed if its owner crashed, kept if live, [else stop].

    The TTL bounds how long a live owner counts as a fetch; it must not keep
    a crashed owner's marker on disk forever.
    MUTATION TARGET: check the TTL before the pid and the crashed marker survives.
    """
    data_dir = tmp_path / "data"
    owners = stem_hydration_module.hydrate_owners_dir(data_dir)
    owners.mkdir(parents=True)
    stale_at = time.time() - stem_hydration_module.HYDRATE_OWNER_TTL_S - 60
    exited = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True
    )
    dead_pid = int(exited.stdout)
    assert not psutil.pid_exists(dead_pid), "control: the exited child's pid must be free"
    crashed = owners / _owner_marker("ttl-track", dead_pid, "deadbeef")
    crashed.write_text(
        json.dumps({"pid": dead_pid, "create_time": stale_at, "started_at": stale_at}),
        encoding="utf-8",
        newline="\n",
    )
    me = psutil.Process()
    live = owners / _owner_marker("ttl-track", me.pid, "cafef00d")
    live.write_text(
        json.dumps({"pid": me.pid, "create_time": me.create_time(), "started_at": stale_at}),
        encoding="utf-8",
        newline="\n",
    )

    assert stem_hydration_module.hydrate_owner_alive(data_dir, "ttl-track") is False
    assert not crashed.exists(), "a crashed owner's marker past the TTL was kept"
    assert live.exists(), "a live owner's marker past the TTL was removed"


@pytest.mark.requirement("STEM-12")
def test_a_malformed_generation_file_is_a_hydration_error(tmp_path: Path):
    """[if] the generation file is not a number or text [then] HydrationError is raised, [else stop].

    A state read catches only HydrationError, so a ValueError or
    UnicodeDecodeError here would crash or 500 every track on that stripe.
    MUTATION TARGET: let the parse or decode error escape and this test fails.
    """
    data_dir = tmp_path / "data"
    path = stem_hydration_module.hydrate_generation_path(data_dir, "gen-track")
    path.parent.mkdir(parents=True)
    for payload in (b"", b"not-a-number", b"\xff\xfe\x00"):
        path.write_bytes(payload)
        with pytest.raises(stem_hydration_module.HydrationError):
            stem_hydration_module.read_hydrate_generation(data_dir, "gen-track")


@pytest.mark.requirement("STEM-12")
def test_an_owner_marker_with_undecodable_bytes_is_removed_not_raised(tmp_path: Path):
    """[if] an owner marker holds undecodable bytes [then] it is removed and ignored, [else stop].

    A decode error is not an OSError or a JSON error, so it must take the same
    cleanup path or every state read of that track becomes a 500.
    MUTATION TARGET: drop UnicodeDecodeError from the corrupt-marker handler and this test raises.
    """
    data_dir = tmp_path / "data"
    owners = stem_hydration_module.hydrate_owners_dir(data_dir)
    owners.mkdir(parents=True)
    marker = owners / _owner_marker("bytes-track", 1, "deadbeef")
    marker.write_bytes(b"\xff\xfe\x00")

    assert stem_hydration_module.hydrate_owner_alive(data_dir, "bytes-track") is False
    assert not marker.exists()


class _LosingSource(DirectR2Source):
    """Fetches for real, then another hydrator publishes the same bundle first."""

    def __init__(self, *, cfg: CloudConfig, s3, stems_dir: Path, stable_id: str) -> None:
        super().__init__(cfg=cfg, s3=s3)
        self._winner = stems_dir / stable_id

    def fetch_bundle_files(self, *, stable_id, file_hashes, tmp_dir):
        total = super().fetch_bundle_files(stable_id=stable_id, file_hashes=file_hashes, tmp_dir=tmp_dir)
        shutil.copytree(tmp_dir, self._winner)
        return total


@pytest.mark.requirement("STEM-58")
def test_a_losing_hydration_copy_that_cannot_be_removed_is_reported_by_name(
    tmp_path: Path, fake_s3, cfg: CloudConfig, monkeypatch: pytest.MonkeyPatch
):
    """[if] a hydrate loses the race and its copy cannot be removed [then] the outcome names that copy, [else stop].

    MUTATION TARGET: ``shutil.rmtree(tmp_dir, ignore_errors=True)`` (the
    pre-fix cleanup), which reported already_local while the whole fetched
    copy stayed in the stem root (Sol on #6820). The winner's bundle is
    local, so the outcome stays already_local either way. Control: a
    removable copy is removed and the outcome carries no reason.
    """
    stems_dir = tmp_path / "stems"
    entry = _seed_bundle(fake_s3, cfg, "raced")

    def hydrate() -> HydrationOutcome:
        shutil.rmtree(stems_dir / "raced", ignore_errors=True)
        return hydrate_one(
            "raced",
            data_dir=tmp_path / "data",
            source=_LosingSource(cfg=cfg, s3=fake_s3, stems_dir=stems_dir, stable_id="raced"),
            index={"raced": entry},
            stems_dir=stems_dir,
        )

    outcome = hydrate()
    assert (outcome.status, outcome.reason) == ("already_local", None)
    assert sorted(p.name for p in stems_dir.iterdir()) == ["raced"], "control: the losing copy is removed"

    real = shutil.rmtree

    def refuse_the_copy(path, *args, **kwargs):
        if "raced" + stem_cache_budget.IN_FLIGHT_MARKER in Path(path).name:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", refuse_the_copy)
    outcome = hydrate()
    [stuck] = [p for p in stems_dir.iterdir() if p.name != "raced"]
    assert outcome.status == "already_local"
    assert outcome.reason is not None and str(stuck) in outcome.reason
