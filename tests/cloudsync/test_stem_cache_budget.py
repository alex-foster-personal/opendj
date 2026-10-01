"""Disk-aware stem cache budget (STEM-39 .. STEM-43).

The stem cache budget is DERIVED from free disk, not a fixed number, and
eviction is gated on the R2 index: a bundle R2 does not hold byte for byte is
the only copy of a render, so it is queued for upload and surfaced, never
removed.

Every test injects the disk measurement. A test that read the real volume
would be asserting on whatever the machine happened to have free that day.

* [if] a bundle is not in the R2 index [then] it is never evicted, however
  short of space the volume is; it is queued for upload instead.
* [if] the volume has at least the floor free [then] nothing is evicted.
* [if] the volume is short by one bundle [then] exactly one bundle goes, the
  least recently used, and eviction stops there.
* [if] a bundle is loaded or playing on a deck [then] it is never evicted.
"""
from __future__ import annotations

import hashlib
import json
import os
import wave
from pathlib import Path

import pytest

from apps.cloud import stem_cache_budget as budget
from apps.cloud.stem_cache_budget import (
    GIB,
    DiskUsage,
    StemCacheSettings,
    StemCacheSettingsError,
)
from apps.cloud.stem_hydration import OPEN_DECKS, OpenDeckRegistry

VOLUME_BYTES: int = 460 * GIB
#: 10% of a 460 GiB volume, which is larger than the 30 GiB absolute floor.
FLOOR_BYTES: int = 46 * GIB


def _wav_bytes(*, frames: int = 8, seed: int = 0) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44_100)
        out.writeframes(bytes([seed % 256, 0]) * frames * 2)
    return buf.getvalue()


def _make_bundle(stems_dir: Path, stable_id: str, *, atime: float) -> dict[str, str]:
    """Write one strict-loadable bundle and return its {filename: sha256}
    entry, i.e. exactly what the push rail would have journaled for it."""
    bundle_dir = stems_dir / stable_id
    bundle_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/music/x.wav", "sha256": "a" * 64},
        "files": {p: f"{p}.wav" for p in ("vocals", "drums", "bass", "other")},
    }
    files = {"manifest.json": (json.dumps(manifest) + "\n").encode("utf-8")}
    for part in ("vocals", "drums", "bass", "other"):
        files[f"{part}.wav"] = _wav_bytes()
    entry: dict[str, str] = {}
    for filename, body in files.items():
        path = bundle_dir / filename
        path.write_bytes(body)
        os.utime(path, (atime, atime))
        entry[filename] = hashlib.sha256(body).hexdigest()
    return entry


def _bundle_bytes(stems_dir: Path, stable_id: str) -> int:
    return sum(f.stat().st_size for f in (stems_dir / stable_id).iterdir())


def _disk(*, free: int) -> DiskUsage:
    return DiskUsage(total_bytes=VOLUME_BYTES, free_bytes=free)


def _enforce(stems_dir: Path, data_dir: Path, **kwargs):
    kwargs.setdefault("protected", frozenset())
    kwargs.setdefault("can_rehydrate", True)
    return budget.enforce(stems_dir, data_dir=data_dir, **kwargs)


# --- the floor and the derived budget (STEM-39) -------------------------------


@pytest.mark.requirement("STEM-39")
def test_floor_is_the_larger_of_the_absolute_floor_and_ten_percent():
    """[if] 10% of the volume exceeds 30 GiB [then] 10% is the floor and otherwise 30 GiB is, [else stop]."""
    settings = StemCacheSettings()
    assert budget.floor_bytes(460 * GIB, settings) == 46 * GIB
    assert budget.floor_bytes(200 * GIB, settings) == 30 * GIB
    assert budget.floor_bytes(300 * GIB, settings) == 30 * GIB


@pytest.mark.requirement("STEM-39")
def test_budget_is_derived_from_free_disk_not_a_fixed_number():
    """[if] free disk changes [then] the budget moves with it, [else stop].

    MUTATION TARGET: return a constant from ``derived_budget_bytes`` and the
    second assertion goes red.
    """
    settings = StemCacheSettings()
    cache = 62 * GIB
    roomy = budget.derived_budget_bytes(cache, _disk(free=100 * GIB), settings)
    tight = budget.derived_budget_bytes(cache, _disk(free=4 * GIB), settings)
    assert roomy == cache + 100 * GIB - FLOOR_BYTES
    assert tight == cache + 4 * GIB - FLOOR_BYTES
    assert tight < cache < roomy
    # A volume so short that even an empty cache cannot reach the floor
    # yields a zero budget, never a negative one.
    assert budget.derived_budget_bytes(GIB, _disk(free=GIB), settings) == 0


@pytest.mark.requirement("STEM-39")
def test_explicit_cache_cap_only_ever_lowers_the_budget():
    """[if] max_cache_gib is set [then] the budget is the smaller of the two, [else stop]."""
    capped = StemCacheSettings(max_cache_gib=10.0)
    assert budget.derived_budget_bytes(62 * GIB, _disk(free=200 * GIB), capped) == 10 * GIB
    assert budget.derived_budget_bytes(62 * GIB, _disk(free=4 * GIB), capped) == 10 * GIB
    # The cap never RAISES a budget the disk has already pulled below it.
    assert budget.derived_budget_bytes(5 * GIB, _disk(free=45 * GIB), capped) == 4 * GIB


# --- eviction gated on the R2 index (STEM-40) ---------------------------------


@pytest.mark.requirement("STEM-40")
def test_local_only_bundle_is_never_evicted_and_is_queued_for_upload(tmp_path: Path):
    """[if] a bundle is absent from the R2 index [then] it survives any disk pressure and lands in the upload queue, [else stop].

    MUTATION TARGET: drop the index check in ``_evict_lru`` and both
    ``exists()`` assertions go red.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    _make_bundle(stems_dir, "local-only-oldest", atime=1_000_000)
    indexed = _make_bundle(stems_dir, "indexed-newer", atime=2_000_000)

    report = _enforce(
        stems_dir, data_dir, index={"indexed-newer": indexed}, disk=_disk(free=0)
    )

    assert (stems_dir / "local-only-oldest").exists()
    assert "local-only-oldest" not in report.evicted_stable_ids
    assert report.evicted_stable_ids == ("indexed-newer",)
    assert "local-only-oldest" in report.queued_for_upload
    queue = budget.load_upload_queue(data_dir)
    assert queue["local-only-oldest"]["reason"] == budget.REASON_NOT_IN_INDEX


@pytest.mark.requirement("STEM-40")
def test_nothing_is_evicted_when_the_index_is_empty(tmp_path: Path):
    """[if] no index is cached at all [then] every bundle is local-only and all of them survive, [else stop].

    An unfetched index must never read as 'everything is safely in R2'.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    _make_bundle(stems_dir, "a", atime=1_000_000)
    _make_bundle(stems_dir, "b", atime=2_000_000)

    report = _enforce(stems_dir, data_dir, index={}, disk=_disk(free=0))

    assert report.evicted_stable_ids == ()
    assert sorted(report.queued_for_upload) == ["a", "b"]
    assert (stems_dir / "a").exists() and (stems_dir / "b").exists()


@pytest.mark.requirement("STEM-40")
def test_rerendered_bundle_whose_bytes_differ_from_the_index_is_never_evicted(
    tmp_path: Path,
):
    """[if] the index names this bundle but with other bytes [then] the local copy is the only copy of THIS render and survives, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    entry = _make_bundle(stems_dir, "rerendered", atime=1_000_000)
    (stems_dir / "rerendered" / "vocals.wav").write_bytes(_wav_bytes(seed=7))
    os.utime(stems_dir / "rerendered" / "vocals.wav", (1_000_000, 1_000_000))

    report = _enforce(stems_dir, data_dir, index={"rerendered": entry}, disk=_disk(free=0))

    assert (stems_dir / "rerendered").exists()
    assert report.evicted_stable_ids == ()
    assert (
        budget.load_upload_queue(data_dir)["rerendered"]["reason"]
        == budget.REASON_CONTENT_DIFFERS
    )


@pytest.mark.requirement("STEM-40")
def test_bundle_with_a_file_the_index_does_not_list_is_never_evicted(tmp_path: Path):
    """[if] a local bundle holds a file the index has no digest for [then] it is treated as local-only, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    entry = _make_bundle(stems_dir, "partial", atime=1_000_000)
    del entry["bass.wav"]

    report = _enforce(stems_dir, data_dir, index={"partial": entry}, disk=_disk(free=0))

    assert (stems_dir / "partial").exists()
    assert report.evicted_stable_ids == ()
    assert "partial" in report.queued_for_upload


@pytest.mark.requirement("STEM-40")
def test_upload_queue_drops_a_bundle_once_the_index_confirms_it(tmp_path: Path):
    """[if] a queued bundle is later published [then] it leaves the queue, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    entry = _make_bundle(stems_dir, "late", atime=1_000_000)
    healthy = _disk(free=200 * GIB)

    _enforce(stems_dir, data_dir, index={}, disk=healthy)
    assert "late" in budget.load_upload_queue(data_dir)

    _enforce(stems_dir, data_dir, index={"late": entry}, disk=healthy)
    assert "late" not in budget.load_upload_queue(data_dir)


# --- healthy disk, and stopping at the floor (STEM-41) -------------------------


@pytest.mark.requirement("STEM-41")
def test_nothing_is_evicted_when_disk_is_healthy(tmp_path: Path):
    """[if] free disk is at or above the floor [then] no bundle is removed, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {
        "old": _make_bundle(stems_dir, "old", atime=1_000_000),
        "new": _make_bundle(stems_dir, "new", atime=2_000_000),
    }

    for free in (FLOOR_BYTES, FLOOR_BYTES + 1, 300 * GIB):
        report = _enforce(stems_dir, data_dir, index=index, disk=_disk(free=free))
        assert report.state == "healthy"
        assert report.evicted_stable_ids == ()
    assert (stems_dir / "old").exists() and (stems_dir / "new").exists()


@pytest.mark.requirement("STEM-41")
def test_eviction_stops_at_the_floor(tmp_path: Path):
    """[if] the volume is short by one bundle [then] exactly the least recently used bundle goes and the rest stay, [else stop].

    MUTATION TARGET: remove the ``freed >= need`` break and all three go.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {
        sid: _make_bundle(stems_dir, sid, atime=atime)
        for sid, atime in (("oldest", 1_000_000), ("middle", 2_000_000), ("newest", 3_000_000))
    }
    one_bundle = _bundle_bytes(stems_dir, "oldest")

    report = _enforce(
        stems_dir, data_dir, index=index, disk=_disk(free=FLOOR_BYTES - one_bundle)
    )

    assert report.evicted_stable_ids == ("oldest",)
    assert report.bytes_freed == one_bundle
    assert not (stems_dir / "oldest").exists()
    assert (stems_dir / "middle").exists() and (stems_dir / "newest").exists()


@pytest.mark.requirement("STEM-41")
def test_short_by_one_byte_more_than_a_bundle_takes_two_and_no_more(tmp_path: Path):
    """[if] the shortfall is one byte past a bundle [then] exactly two bundles go, [else stop].

    The opposite-direction control for the floor: a shortfall one byte
    past a bundle must take the second bundle, so a fix that stops early is
    caught as surely as one that never stops.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {
        sid: _make_bundle(stems_dir, sid, atime=atime)
        for sid, atime in (("oldest", 1_000_000), ("middle", 2_000_000), ("newest", 3_000_000))
    }
    one_bundle = _bundle_bytes(stems_dir, "oldest")

    report = _enforce(
        stems_dir, data_dir, index=index, disk=_disk(free=FLOOR_BYTES - one_bundle - 1)
    )

    assert report.evicted_stable_ids == ("oldest", "middle")
    assert (stems_dir / "newest").exists()


@pytest.mark.requirement("STEM-41")
def test_dry_run_reports_the_plan_and_removes_nothing(tmp_path: Path):
    """[if] dry_run [then] the report names what would go and disk is untouched, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"old": _make_bundle(stems_dir, "old", atime=1_000_000)}

    report = _enforce(stems_dir, data_dir, index=index, disk=_disk(free=0), dry_run=True)

    assert report.dry_run is True
    assert report.evicted_stable_ids == ("old",)
    assert (stems_dir / "old").exists()
    assert not budget.upload_queue_path(data_dir).exists()


# --- loaded or playing bundles (STEM-42) ---------------------------------------


@pytest.mark.requirement("STEM-42")
def test_a_bundle_open_on_a_deck_is_never_evicted(tmp_path: Path):
    """[if] the least recently used bundle is open on a deck [then] it stays and the next one goes, [else stop].

    MUTATION TARGET: pass ``protected=frozenset()`` and ``on-deck`` is removed.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {
        "on-deck": _make_bundle(stems_dir, "on-deck", atime=1_000_000),
        "idle": _make_bundle(stems_dir, "idle", atime=2_000_000),
    }
    registry = OpenDeckRegistry()
    registry.mark_open("on-deck")

    report = _enforce(
        stems_dir, data_dir, index=index, disk=_disk(free=0), protected=registry.open_ids()
    )

    assert (stems_dir / "on-deck").exists()
    assert report.evicted_stable_ids == ("idle",)
    assert report.protected_count == 1


@pytest.mark.requirement("STEM-42")
def test_a_bundle_being_served_to_a_playing_deck_is_never_evicted(tmp_path: Path):
    """[if] the stems routes served this bundle within the TTL [then] it is protected with no explicit deck-open call, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"playing": _make_bundle(stems_dir, "playing", atime=1_000_000)}
    registry = OpenDeckRegistry()
    registry.mark_served("playing", now=100.0)

    report = _enforce(
        stems_dir,
        data_dir,
        index=index,
        disk=_disk(free=0),
        protected=registry.open_ids(now=101.0),
    )

    assert (stems_dir / "playing").exists()
    assert report.evicted_stable_ids == ()
    assert report.blocked_reason == budget.BLOCKED_NOTHING_EVICTABLE


# --- containment: no hydration, no eviction (STEM-41) --------------------------


@pytest.mark.requirement("STEM-41")
def test_nothing_is_evicted_when_this_machine_cannot_rehydrate(tmp_path: Path):
    """[if] no hydration source is armed [then] an R2-confirmed bundle stays and the report says why, [else stop].

    Evicting it would leave the deck with no way to get the bundle back.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"old": _make_bundle(stems_dir, "old", atime=1_000_000)}

    report = _enforce(
        stems_dir, data_dir, index=index, disk=_disk(free=0), can_rehydrate=False
    )

    assert (stems_dir / "old").exists()
    assert report.evicted_stable_ids == ()
    assert report.state == "low_disk"
    assert report.blocked_reason == budget.BLOCKED_HYDRATION_NOT_ARMED


@pytest.mark.requirement("STEM-43")
def test_auto_evict_off_surfaces_low_disk_without_removing_anything(tmp_path: Path):
    """[if] the auto_evict setting is off [then] low disk is reported only, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"old": _make_bundle(stems_dir, "old", atime=1_000_000)}

    report = _enforce(
        stems_dir,
        data_dir,
        index=index,
        disk=_disk(free=0),
        settings=StemCacheSettings(auto_evict=False),
    )

    assert (stems_dir / "old").exists()
    assert report.blocked_reason == budget.BLOCKED_AUTO_EVICT_OFF


# --- status (STEM-43) ------------------------------------------------------------


@pytest.mark.requirement("STEM-43")
def test_status_reports_low_disk_with_the_numbers_behind_it(tmp_path: Path):
    """[if] free disk is under the floor [then] status says low_disk and carries free, floor, shortfall and the local-only count, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    indexed = _make_bundle(stems_dir, "indexed", atime=1_000_000)
    _make_bundle(stems_dir, "local-only", atime=2_000_000)

    status = budget.status(
        stems_dir,
        data_dir=data_dir,
        index={"indexed": indexed},
        protected=frozenset(),
        can_rehydrate=True,
        disk=_disk(free=4 * GIB),
    )

    assert status["state"] == "low_disk"
    assert status["disk_free_bytes"] == 4 * GIB
    assert status["floor_bytes"] == FLOOR_BYTES
    assert status["shortfall_bytes"] == FLOOR_BYTES - 4 * GIB
    assert status["bundle_count"] == 2
    assert status["local_only_count"] == 1
    assert status["local_only_stable_ids"] == ["local-only"]
    assert status["evictable_bundle_count"] == 1
    # One small indexed bundle cannot cover a 42 GiB shortfall.
    assert status["blocked_reason"] == budget.BLOCKED_NOTHING_EVICTABLE


@pytest.mark.requirement("STEM-43")
def test_status_is_healthy_and_unblocked_above_the_floor(tmp_path: Path):
    """[if] free disk is above the floor [then] status reads healthy with no blocker, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    status = budget.status(
        stems_dir,
        data_dir=data_dir,
        index={},
        protected=frozenset(),
        can_rehydrate=False,
        disk=_disk(free=200 * GIB),
    )
    assert status["state"] == "healthy"
    assert status["shortfall_bytes"] == 0
    assert status["blocked_reason"] is None
    assert status["bundle_count"] == 0


@pytest.mark.requirement("STEM-43")
def test_status_never_writes(tmp_path: Path):
    """[if] status is read [then] no queue or settings file appears on disk, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    _make_bundle(stems_dir, "local-only", atime=1_000_000)
    budget.status(
        stems_dir, data_dir=data_dir, index={}, protected=frozenset(),
        can_rehydrate=True, disk=_disk(free=0),
    )
    assert not (data_dir / "state").exists()
    assert (stems_dir / "local-only").exists()


# --- settings (STEM-43) ----------------------------------------------------------


@pytest.mark.requirement("STEM-43")
def test_settings_default_when_no_file_and_round_trip_when_saved(tmp_path: Path):
    """[if] no settings file exists [then] defaults load, and a saved override reads back, [else stop]."""
    assert budget.load_settings(tmp_path) == StemCacheSettings()
    saved = StemCacheSettings(floor_gib=50.0, floor_fraction=0.2, max_cache_gib=20.0)
    budget.save_settings(tmp_path, saved)
    assert budget.load_settings(tmp_path) == saved
    assert budget.floor_bytes(100 * GIB, saved) == 50 * GIB


@pytest.mark.requirement("STEM-43")
@pytest.mark.parametrize(
    "payload",
    [
        {"floor_gib": -1},
        {"floor_fraction": 1.0},
        {"floor_fraction": -0.1},
        {"max_cache_gib": -5},
        {"enforce_interval_s": 0},
        {"auto_evict": "yes"},
        {"floor_gb": 30},
    ],
)
def test_malformed_settings_fail_loud(tmp_path: Path, payload: dict[str, object]):
    """[if] the settings file holds a bad or unknown value [then] loading raises, never falls back to defaults, [else stop]."""
    path = budget.settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StemCacheSettingsError):
        budget.load_settings(tmp_path)


@pytest.mark.requirement("STEM-43")
def test_saved_floor_override_changes_what_enforcement_does(tmp_path: Path):
    """[if] the floor is lowered in settings [then] a volume that was low is healthy and nothing is evicted, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"old": _make_bundle(stems_dir, "old", atime=1_000_000)}
    budget.save_settings(data_dir, StemCacheSettings(floor_gib=1.0, floor_fraction=0.0))

    report = _enforce(stems_dir, data_dir, index=index, disk=_disk(free=2 * GIB))

    assert report.state == "healthy"
    assert (stems_dir / "old").exists()


# --- wiring into hydrate_one (STEM-39) --------------------------------------------


@pytest.mark.requirement("STEM-39")
def test_hydrate_one_enforces_the_disk_budget_after_it_lands(
    tmp_path: Path, fake_s3, cfg, monkeypatch: pytest.MonkeyPatch
):
    """[if] a hydrate lands on a low disk [then] only an older R2-confirmed bundle is evicted, [else stop].

    A local-only bundle is kept and the bundle just hydrated survives.
    """
    from apps.cloud.asset_store import asset_object_key
    from apps.cloud.stem_hydration import hydrate_one
    from apps.cloud.stem_source import DirectR2Source

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    old_entry = _make_bundle(stems_dir, "old-indexed", atime=1_000_000)
    _make_bundle(stems_dir, "old-local-only", atime=1_000_001)

    new_dir = tmp_path / "seed"
    new_entry = _make_bundle(new_dir, "new-track", atime=1_000_000)
    for filename, digest in new_entry.items():
        fake_s3.put_object_if_none_match(
            cfg.audio_bucket,
            asset_object_key(digest),
            (new_dir / "new-track" / filename).read_bytes(),
        )
    monkeypatch.setattr(budget, "measure_disk", lambda _path: _disk(free=0))

    outcome = hydrate_one(
        "new-track",
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={"new-track": new_entry, "old-indexed": old_entry},
        stems_dir=stems_dir,
    )

    assert outcome.status == "hydrated"
    assert (stems_dir / "new-track" / "manifest.json").exists()
    assert not (stems_dir / "old-indexed").exists()
    assert (stems_dir / "old-local-only").exists()
    assert not OPEN_DECKS.is_open("new-track")


@pytest.mark.requirement("STEM-39")
def test_hydrate_one_gives_back_only_what_it_took(
    tmp_path: Path, fake_s3, cfg, monkeypatch: pytest.MonkeyPatch
):
    """[if] a hydrate lands on a large backlog [then] it evicts about one bundle's worth, not the backlog, [else stop].

    A deck is waiting on it and the timer owns the rest.

    MUTATION TARGET: drop ``max_evict_bytes=total`` and all three old
    bundles go.
    """
    from apps.cloud.asset_store import asset_object_key
    from apps.cloud.stem_hydration import hydrate_one
    from apps.cloud.stem_source import DirectR2Source

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    # Ids the same length as "new-track": the manifest embeds the id, so
    # equal-length ids make every bundle here exactly the same size and the
    # bound "what this hydrate took" equal to exactly one old bundle.
    index = {
        sid: _make_bundle(stems_dir, sid, atime=atime)
        for sid, atime in (("old-track", 1_000_000), ("mid-track", 2_000_000), ("top-track", 3_000_000))
    }
    new_entry = _make_bundle(tmp_path / "seed", "new-track", atime=1_000_000)
    for filename, digest in new_entry.items():
        fake_s3.put_object_if_none_match(
            cfg.audio_bucket,
            asset_object_key(digest),
            (tmp_path / "seed" / "new-track" / filename).read_bytes(),
        )
    monkeypatch.setattr(budget, "measure_disk", lambda _path: _disk(free=0))

    outcome = hydrate_one(
        "new-track",
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={**index, "new-track": new_entry},
        stems_dir=stems_dir,
    )

    assert outcome.status == "hydrated"
    assert not (stems_dir / "old-track").exists()
    assert (stems_dir / "mid-track").exists() and (stems_dir / "top-track").exists()


@pytest.mark.requirement("STEM-41")
def test_max_evict_bytes_bounds_one_pass_and_the_next_pass_continues(tmp_path: Path):
    """[if] a pass is bounded [then] it stops at the bound, and an unbounded pass afterwards finishes the job, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {
        sid: _make_bundle(stems_dir, sid, atime=atime)
        for sid, atime in (("oldest", 1_000_000), ("middle", 2_000_000), ("newest", 3_000_000))
    }
    one_bundle = _bundle_bytes(stems_dir, "oldest")

    bounded = _enforce(
        stems_dir, data_dir, index=index, disk=_disk(free=0), max_evict_bytes=one_bundle
    )
    assert bounded.evicted_stable_ids == ("oldest",)

    unbounded = _enforce(stems_dir, data_dir, index=index, disk=_disk(free=0))
    assert unbounded.evicted_stable_ids == ("middle", "newest")
