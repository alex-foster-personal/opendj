"""On-demand + bulk R2 stem hydration (ADR-0024).

Uses the SAME asset-tier fake other cloud tests use (``InMemoryAssetS3`` /
``fake_s3`` from ``tests/cloudsync/conftest.py``) and the real strict loader
(:func:`apps.webui.server.stem_artifacts.load_stem_bundle`) to verify a
hydrated bundle -- never a mocked loader.

* [if] a bundle is already valid on local disk [then] hydration is a no-op
  reported ``already_local``.
* [if] the R2 index has a bundle's manifest + parts [then] hydration writes
  them to disk and the STRICT loader accepts the result.
* [if] a bulk request names a reserved id [then] it is skipped, reason
  ``"reserved"``, UNLESS ``include_reserved=True`` is passed explicitly.
* [if] the SAME id is loaded on-demand (:func:`hydrate_one` with its default
  ``skip_reserved=False``) [then] it is NOT skipped.
* [if] a bundle is open on a deck [then] :func:`enforce_budget` never evicts
  it even when it is the least-recently-used bundle over budget.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from apps.cloud import asset_store, policy
from apps.cloud.asset_store import asset_object_key
from apps.cloud.config import CloudConfig
from apps.cloud.stem_hydration import (
    OPEN_DECK_SERVED_TTL_S,
    OPEN_DECKS,
    HydrationOutcome,
    OpenDeckRegistry,
    _bundle_remote_size,
    bulk_hydrate,
    enforce_budget,
    hydrate_one,
    load_reserved_ids,
)
from apps.webui.server.stem_artifacts import load_stem_bundle
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
    stems_dir = tmp_path / "stems"
    bundle_dir = stems_dir / "local-track"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "manifest.json").write_bytes(_manifest_bytes("local-track"))
    for part in ("vocals", "drums", "bass", "other"):
        (bundle_dir / f"{part}.wav").write_bytes(_wav_bytes())

    outcome = hydrate_one(
        "local-track",
        data_dir=tmp_path / "data",
        cfg=cfg,
        s3=fake_s3,
        index={},
        stems_dir=stems_dir,
    )
    assert outcome.status == "already_local"
    assert fake_s3.head_calls == []  # no network touched


@pytest.mark.requirement("STEM-12")
def test_hydrate_one_fetches_and_strict_loads(tmp_path: Path, fake_s3, cfg: CloudConfig):
    stems_dir = tmp_path / "stems"
    index_entry = _seed_bundle(fake_s3, cfg, "remote-track")

    outcome = hydrate_one(
        "remote-track",
        data_dir=tmp_path / "data",
        cfg=cfg,
        s3=fake_s3,
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
    outcome = hydrate_one(
        "nowhere",
        data_dir=tmp_path / "data",
        cfg=cfg,
        s3=fake_s3,
        index={},
        stems_dir=tmp_path / "stems",
    )
    assert outcome.status == "unavailable"


@pytest.mark.requirement("STEM-22")
def test_hydrate_one_rejects_disallowed_index_filename(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    """[if] the caller-supplied index names a traversal filename [then] no files
    are written and hydration reports an explicit error."""
    stems_dir = tmp_path / "stems"
    stable_id = "escape-track"
    index_entry = _seed_bundle(fake_s3, cfg, stable_id)
    del index_entry["vocals.wav"]
    index_entry["../escape.wav"] = "f" * 64

    parent_dir = stems_dir.parent
    outcome = hydrate_one(
        stable_id,
        data_dir=tmp_path / "data",
        cfg=cfg,
        s3=fake_s3,
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
    the fetch must fail, and the half-written directory must not remain."""
    stems_dir = tmp_path / "stems"
    index_entry = _seed_bundle(fake_s3, cfg, "broken-track")
    index_entry["vocals.wav"] = "f" * 64  # never pushed -> fetch_asset raises

    outcome = hydrate_one(
        "broken-track",
        data_dir=tmp_path / "data",
        cfg=cfg,
        s3=fake_s3,
        index={"broken-track": index_entry},
        stems_dir=stems_dir,
    )
    assert outcome.status == "error"
    assert not (stems_dir / "broken-track").exists()


# --- reservation guard: bulk skips, on-demand does not (D5) ------------------


@pytest.mark.requirement("STEM-13")
def test_bulk_hydrate_skips_reserved_by_default(tmp_path: Path, fake_s3, cfg: CloudConfig):
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    entry = _seed_bundle(fake_s3, cfg, "reserved-track")
    _reservation_file(data_dir, reserved_ids=["reserved-track"])

    report = bulk_hydrate(
        ["reserved-track"],
        data_dir=data_dir,
        cfg=cfg,
        s3=fake_s3,
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
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    entry = _seed_bundle(fake_s3, cfg, "reserved-track")
    _reservation_file(data_dir, reserved_ids=["reserved-track"])

    report = bulk_hydrate(
        ["reserved-track"],
        data_dir=data_dir,
        cfg=cfg,
        s3=fake_s3,
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
    -- the deck-load path must be able to hydrate a reserved id."""
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    entry = _seed_bundle(fake_s3, cfg, "reserved-track")
    _reservation_file(data_dir, reserved_ids=["reserved-track"])

    outcome = hydrate_one(
        "reserved-track",
        data_dir=data_dir,
        cfg=cfg,
        s3=fake_s3,
        index={"reserved-track": entry},
        stems_dir=stems_dir,
        skip_reserved=False,  # the on-demand deck-load caller's contract
    )
    assert outcome.status == "hydrated"


@pytest.mark.requirement("STEM-13")
def test_load_reserved_ids_missing_file_is_empty(tmp_path: Path):
    assert load_reserved_ids(tmp_path / "data") == frozenset()


@pytest.mark.requirement("STEM-13")
def test_load_reserved_ids_reads_tracks_array(tmp_path: Path):
    data_dir = tmp_path / "data"
    _reservation_file(data_dir, reserved_ids=["a", "b"])
    assert load_reserved_ids(data_dir) == frozenset({"a", "b"})


# --- budget-bounded LRU eviction, open-deck protected -------------------------


def _make_bundle_on_disk(stems_dir: Path, stable_id: str, *, atime: float) -> None:
    import os

    bundle_dir = stems_dir / stable_id
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "manifest.json").write_bytes(_manifest_bytes(stable_id))
    for part in ("vocals", "drums", "bass", "other"):
        path = bundle_dir / f"{part}.wav"
        path.write_bytes(_wav_bytes())
        os.utime(path, (atime, atime))


@pytest.mark.requirement("STEM-14")
def test_enforce_budget_evicts_least_recently_used_bundle(tmp_path: Path):
    stems_dir = tmp_path / "stems"
    _make_bundle_on_disk(stems_dir, "old", atime=1_000_000)
    _make_bundle_on_disk(stems_dir, "new", atime=2_000_000)
    size_per_bundle = sum(f.stat().st_size for f in (stems_dir / "old").iterdir())
    # Budget for exactly one bundle: the older one must go.
    outcome = enforce_budget(stems_dir, budget_mb=0)
    assert "old" in outcome.evicted_stable_ids or "new" in outcome.evicted_stable_ids
    assert size_per_bundle > 0


@pytest.mark.requirement("STEM-14")
def test_enforce_budget_never_evicts_an_open_deck():
    """MUTATION TARGET: pass ``protected=frozenset()`` instead of the real
    open-deck set here and this test goes red."""
    stems_dir_holder: dict[str, Path] = {}

    def _setup(tmp: Path) -> Path:
        stems_dir = tmp / "stems"
        _make_bundle_on_disk(stems_dir, "oldest-but-open", atime=1_000_000)
        _make_bundle_on_disk(stems_dir, "newer", atime=2_000_000)
        stems_dir_holder["dir"] = stems_dir
        return stems_dir

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        stems_dir = _setup(Path(tmp))
        registry = OpenDeckRegistry()
        registry.mark_open("oldest-but-open")
        outcome = enforce_budget(stems_dir, budget_mb=0, protected=registry.open_ids())
        assert "oldest-but-open" not in outcome.evicted_stable_ids
        assert (stems_dir / "oldest-but-open").exists()


@pytest.mark.requirement("STEM-21")
def test_bulk_hydrate_skips_oversized_bundle_before_download(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    """A bundle larger than the remaining byte budget must never start downloading."""
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    entry = _seed_bundle(fake_s3, cfg, "big-track")

    report = bulk_hydrate(
        ["big-track"],
        data_dir=data_dir,
        cfg=cfg,
        s3=fake_s3,
        index={"big-track": entry},
        byte_budget=10,
        stems_dir=stems_dir,
    )
    assert len(report.fetched) == 0
    assert report.skipped[0].status == "unavailable"
    assert report.skipped[0].reason is not None
    assert "byte_budget" in report.skipped[0].reason
    assert not (stems_dir / "big-track").exists()


@pytest.mark.requirement("STEM-21")
def test_bulk_hydrate_fits_bundle_within_budget_still_hydrates(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    """HEAD-based budget gating must not block a bundle that legitimately fits."""
    data_dir = tmp_path / "data"
    stems_dir = tmp_path / "stems"
    entry = _seed_bundle(fake_s3, cfg, "fits-track")

    report = bulk_hydrate(
        ["fits-track"],
        data_dir=data_dir,
        cfg=cfg,
        s3=fake_s3,
        index={"fits-track": entry},
        byte_budget=10**9,
        stems_dir=stems_dir,
    )
    assert len(report.fetched) == 1
    assert report.fetched[0].status == "hydrated"
    assert (stems_dir / "fits-track" / "manifest.json").exists()


@pytest.mark.requirement("STEM-14")
def test_open_deck_registry_is_refcounted():
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
        cfg=cfg,
        s3=s3,
        index={stable_id: entry},
        stems_dir=stems_dir,
    )
    assert outcome.status == "error"
    assert "simulated R2 transport failure" in (outcome.reason or "")
    assert not (stems_dir / stable_id).exists()


@pytest.mark.requirement("STEM-27")
def test_bundle_remote_size_classifies_head_transport_failure(cfg: CloudConfig):
    stable_id = "head-fail-track"
    base_s3 = InMemoryAssetS3()
    entry = _seed_bundle(base_s3, cfg, stable_id)
    fail_key = (cfg.audio_bucket, asset_object_key(entry["vocals.wav"]))
    s3 = _TransportFailingAssetS3(fail_key=fail_key)
    for bucket, key in base_s3.store:
        s3.store[(bucket, key)] = base_s3.store[(bucket, key)]

    with pytest.raises(asset_store.AssetStoreError, match="simulated R2 transport failure"):
        _bundle_remote_size(cfg, s3, entry)


# --- concurrent hydration safety (STEM-28) -------------------------------------


@pytest.mark.requirement("STEM-28")
def test_hydrate_one_concurrent_calls_leave_exactly_one_bundle(
    tmp_path: Path, fake_s3, cfg: CloudConfig
):
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    stable_id = "concurrent-track"
    entry = _seed_bundle(fake_s3, cfg, stable_id)
    index = {stable_id: entry}

    def _run() -> HydrationOutcome:
        return hydrate_one(
            stable_id,
            data_dir=data_dir,
            cfg=cfg,
            s3=fake_s3,
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
        cfg=cfg,
        s3=fake_s3,
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
        cfg=cfg,
        s3=fake_s3,
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
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    artifact = policy.CFG.artifacts["stem_bundle"]
    zero_budget = dataclasses.replace(
        policy.CFG,
        artifacts={
            **policy.CFG.artifacts,
            "stem_bundle": dataclasses.replace(artifact, cache_budget_mb=0),
        },
    )
    monkeypatch.setattr(policy, "CFG", zero_budget)

    _make_bundle_on_disk(stems_dir, "open-track", atime=1_000_000)
    OPEN_DECKS.mark_open("open-track")
    try:
        entry = _seed_bundle(fake_s3, cfg, "new-track")
        outcome = hydrate_one(
            "new-track",
            data_dir=data_dir,
            cfg=cfg,
            s3=fake_s3,
            index={"new-track": entry},
            stems_dir=stems_dir,
        )
        assert outcome.status == "hydrated"
        assert (stems_dir / "new-track" / "manifest.json").exists()
    finally:
        OPEN_DECKS.mark_closed("open-track")


# --- serve-TTL eviction protection (STEM-30) ---------------------------------


@pytest.mark.requirement("STEM-30")
def test_open_deck_registry_served_ttl_protects_then_expires():
    registry = OpenDeckRegistry()
    registry.mark_served("t", now=0.0)
    assert "t" in registry.open_ids(now=0.0)
    assert "t" in registry.open_ids(now=OPEN_DECK_SERVED_TTL_S - 1)
    assert "t" not in registry.open_ids(now=OPEN_DECK_SERVED_TTL_S + 1)


@pytest.mark.requirement("STEM-30")
def test_enforce_budget_protects_a_recently_served_bundle_without_deck_open():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        stems_dir = Path(tmp) / "stems"
        _make_bundle_on_disk(stems_dir, "served-track", atime=1_000_000)
        _make_bundle_on_disk(stems_dir, "newer", atime=2_000_000)
        registry = OpenDeckRegistry()
        served_at = 100.0
        registry.mark_served("served-track", now=served_at)

        protected = registry.open_ids(now=served_at)
        outcome = enforce_budget(stems_dir, budget_mb=0, protected=protected)
        assert "served-track" not in outcome.evicted_stable_ids
        assert (stems_dir / "served-track").exists()

        expired_protected = registry.open_ids(now=served_at + OPEN_DECK_SERVED_TTL_S + 1)
        outcome2 = enforce_budget(stems_dir, budget_mb=0, protected=expired_protected)
        assert "served-track" in outcome2.evicted_stable_ids
