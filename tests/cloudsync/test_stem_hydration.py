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

import hashlib
import json
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from apps.cloud import asset_store, stem_cache_budget
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
    STEM_HUB_INDEX_FAILED,
    DirectR2Source,
    StemSourceError,
)
from apps.stems.artifacts import load_stem_bundle
from tests.cloudsync.conftest import InMemoryAssetS3


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
    # The filename check runs before any filesystem write, so the whole
    # stems_dir tree (not just the bundle) must never have been created.
    assert not stems_dir.exists()
    assert list(parent_dir.iterdir()) == []


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
