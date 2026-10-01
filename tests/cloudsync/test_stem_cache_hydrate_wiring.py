"""Stem cache budget wired into hydrate_one (STEM-39).

* [if] a hydrate lands on a low disk [then] only an older R2-confirmed bundle
  is evicted; a local-only bundle and the bundle just hydrated both survive.
* [if] a hydrate lands on a large backlog [then] it gives back only about
  what it took, and the timer owns the rest.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.cloud import stem_cache_budget as budget
from apps.cloud.stem_hydration import OPEN_DECKS
from tests.cloudsync.stem_cache_rig import disk_usage, make_bundle


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
    old_entry = make_bundle(stems_dir, "old-indexed", atime=1_000_000)
    make_bundle(stems_dir, "old-local-only", atime=1_000_001)

    new_dir = tmp_path / "seed"
    new_entry = make_bundle(new_dir, "new-track", atime=1_000_000)
    for filename, digest in new_entry.items():
        fake_s3.put_object_if_none_match(
            cfg.audio_bucket,
            asset_object_key(digest),
            (new_dir / "new-track" / filename).read_bytes(),
        )
    monkeypatch.setattr(budget, "measure_disk", lambda _path: disk_usage(free=0))

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
        sid: make_bundle(stems_dir, sid, atime=atime)
        for sid, atime in (("old-track", 1_000_000), ("mid-track", 2_000_000), ("top-track", 3_000_000))
    }
    new_entry = make_bundle(tmp_path / "seed", "new-track", atime=1_000_000)
    for filename, digest in new_entry.items():
        fake_s3.put_object_if_none_match(
            cfg.audio_bucket,
            asset_object_key(digest),
            (tmp_path / "seed" / "new-track" / filename).read_bytes(),
        )
    monkeypatch.setattr(budget, "measure_disk", lambda _path: disk_usage(free=0))

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
