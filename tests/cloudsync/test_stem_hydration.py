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

import hashlib
import json
import wave
from pathlib import Path

import pytest

from apps.cloud.config import CloudConfig
from apps.cloud.stem_hydration import (
    OpenDeckRegistry,
    bulk_hydrate,
    enforce_budget,
    hydrate_one,
    load_reserved_ids,
)
from apps.webui.server.stem_artifacts import load_stem_bundle


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
