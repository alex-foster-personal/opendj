"""Hydrate pins and the cross-process upload-queue lock (STEM-40, STEM-42).

Two blocking findings from Codex's review of #4974 (a8c805694) and one from
the follow-up review, each with the test that bites and its overshoot
control:

* [if] an eviction pass in any process runs while a hydrate publishes a
  bundle [then] the hydrate's own pin keeps it until the post-hydrate pass.
* [if] a second hydrate of the same id finds it already local [then] the
  first hydrate's pin stays.
* [if] another process holds the upload queue's file lock [then] a save
  waits and merges what that holder wrote.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from apps.cloud import stem_cache_budget as budget
from apps.cloud.stem_cache_settings import write_json_atomically
from apps.cloud.stem_hydration import OpenDeckRegistry
from tests.cloudsync.stem_cache_rig import FLOOR_BYTES, disk_usage, enforce, make_bundle


@pytest.mark.requirement("STEM-42")
def test_a_bundle_a_hydrate_pinned_is_not_evicted_by_any_pass(tmp_path: Path):
    """[if] a fresh hydrate pin names a bundle [then] eviction skips it, [else stop].

    MUTATION TARGET: drop the ``is_hydrate_pinned`` check in ``_evict_lru``
    and the just-published bundle is removed while no deck registry names it.
    """
    from apps.cloud.stem_bundles import pin_for_hydrate

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"publishing": make_bundle(stems_dir, "publishing", atime=100.0)}
    pin_for_hydrate(stems_dir / "publishing")

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=0))

    assert report.evicted_stable_ids == ()
    assert (stems_dir / "publishing").is_dir()


@pytest.mark.requirement("STEM-42")
def test_a_stale_hydrate_pin_is_removed_and_does_not_block_eviction(tmp_path: Path):
    """[if] a pin outlived its TTL (a crashed hydrate) [then] eviction proceeds and the pin is removed, [else stop].

    Overshoot control: a pin that never expired would make a bundle
    unevictable forever after one crash.
    """
    from apps.cloud.stem_bundles import HYDRATE_PIN_TTL_S, pin_for_hydrate

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"crashed": make_bundle(stems_dir, "crashed", atime=100.0)}
    pin = pin_for_hydrate(stems_dir / "crashed")
    old = pin.stat().st_mtime - HYDRATE_PIN_TTL_S - 1
    os.utime(pin, (old, old))

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert report.evicted_stable_ids == ("crashed",)
    assert not pin.exists()


@pytest.mark.requirement("STEM-42")
def test_a_second_hydrate_of_the_same_id_never_drops_the_first_one_s_pin(tmp_path: Path):
    """[if] two processes hydrate one id and the second finds it already local [then] the first one's pin stays, [else stop].

    MUTATION TARGET: share one pin file per id and the second hydrate's
    cleanup unpins the first, leaving its publish window open to eviction.
    """
    from apps.cloud.stem_bundles import is_hydrate_pinned, pin_for_hydrate, unpin_hydrate

    bundle_dir = tmp_path / "stems" / "shared"
    bundle_dir.parent.mkdir(parents=True)
    first = pin_for_hydrate(bundle_dir)
    second = pin_for_hydrate(bundle_dir)
    unpin_hydrate(second)  # the second process gives up: already local

    assert is_hydrate_pinned(bundle_dir)
    unpin_hydrate(first)
    assert not is_hydrate_pinned(bundle_dir), "control: the owner's own unpin releases it"


class _CopySource:
    """A hydration source that copies a prepared bundle into the temp dir."""

    def __init__(self, remote: Path) -> None:
        self._remote = remote

    def fetch_bundle_files(self, *, stable_id: str, file_hashes: object, tmp_dir: Path) -> int:
        total = 0
        for path in sorted((self._remote / stable_id).iterdir()):
            body = path.read_bytes()
            (tmp_dir / path.name).write_bytes(body)
            total += len(body)
        return total


@pytest.mark.requirement("STEM-42")
def test_a_timer_pass_landing_between_publish_and_verify_keeps_the_hydrate(tmp_path: Path, monkeypatch):
    """[if] an eviction pass runs between the publishing rename and the verify [then] the hydrate still succeeds, [else stop].

    MUTATION TARGET: pin after the rename (or not at all) in ``hydrate_one``
    and the timer evicts the bundle, so the verify records a valid fetch as
    a hydration failure.
    """
    from apps.cloud import stem_hydration
    from apps.cloud.stem_bundles import hydrate_pins

    remote, stems_dir, data_dir = tmp_path / "remote", tmp_path / "stems", tmp_path / "data"
    index = {"fresh": make_bundle(remote, "fresh", atime=100.0)}
    real_load = stem_hydration.load_stem_bundle
    timer_reports: list[budget.EnforceReport] = []

    def load_after_a_timer_pass(stable_id: str, *, stems_dir: Path):  # type: ignore[no-untyped-def]
        if not timer_reports and (stems_dir / stable_id).is_dir():
            timer_reports.append(enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=0)))
        return real_load(stable_id, stems_dir=stems_dir)

    monkeypatch.setattr(stem_hydration, "load_stem_bundle", load_after_a_timer_pass)

    outcome = stem_hydration.hydrate_one(
        "fresh", data_dir=data_dir, source=_CopySource(remote), index=index, stems_dir=stems_dir,  # type: ignore[arg-type]
    )

    assert len(timer_reports) == 1, "the timer pass ran inside the publish window"
    assert timer_reports[0].evicted_stable_ids == ()
    assert outcome.status == "hydrated", outcome.reason
    assert (stems_dir / "fresh").is_dir()
    assert hydrate_pins(stems_dir / "fresh") == [], "a drain hydrate leaves it evictable"
    assert "fresh" not in stem_hydration.OPEN_DECKS.open_ids()


@pytest.mark.requirement("STEM-42")
def test_a_deck_load_hydrate_hands_the_bundle_to_the_served_registry(tmp_path: Path):
    """[if] the deck-load path hydrates a bundle [then] it is marked served before the pin is dropped, [else stop].

    MUTATION TARGET: drop the ``hand_off_to_deck`` branch in ``hydrate_one``
    and the bundle is unprotected between this call and the deck's next poll.
    """
    from apps.cloud import stem_hydration
    from apps.cloud.stem_bundles import hydrate_pins

    remote, stems_dir, data_dir = tmp_path / "remote", tmp_path / "stems", tmp_path / "data"
    index = {"for-deck": make_bundle(remote, "for-deck", atime=100.0)}
    registry = OpenDeckRegistry()
    original = stem_hydration.OPEN_DECKS
    stem_hydration.OPEN_DECKS = registry
    try:
        outcome = stem_hydration.hydrate_one(
            "for-deck", data_dir=data_dir, source=_CopySource(remote), index=index,  # type: ignore[arg-type]
            stems_dir=stems_dir, hand_off_to_deck=True,
        )
    finally:
        stem_hydration.OPEN_DECKS = original
    assert outcome.status == "hydrated", outcome.reason
    assert "for-deck" in registry.open_ids()
    assert hydrate_pins(stems_dir / "for-deck") == []


@pytest.mark.requirement("STEM-40")
def test_a_save_in_another_process_waits_for_the_queue_file_lock(tmp_path: Path):
    """[if] another process holds the upload queue's file lock [then] a save waits and merges what that holder wrote, [else stop].

    MUTATION TARGET: drop ``upload_queue_file_lock`` from ``save_upload_queue``
    and the child writes at once, then this process's write erases its entry.
    """
    import subprocess
    import sys
    import time

    data_dir, stems_dir = tmp_path / "data", tmp_path / "stems"
    for stable_id in ("child", "holder"):
        (stems_dir / stable_id).mkdir(parents=True)
    entry: dict[str, object] = {"reason": "not_in_index", "bytes": 1, "queued_at": "t"}
    child_code = (
        "import sys; from pathlib import Path\n"
        "from apps.cloud.stem_bundles import current_fingerprint\n"
        "from apps.cloud.stem_upload_queue import save_upload_queue\n"
        "d, s = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "seen = {'child': current_fingerprint(s / 'child') or 'gone'}\n"
        "save_upload_queue(d, {'child': {'reason': 'not_in_index', 'bytes': 1, 'queued_at': 't'}},"
        " {}, stems_dir=s, seen=seen)\n"
    )
    repo_root = Path(__file__).resolve().parents[2]
    from apps.cloud.stem_upload_queue import upload_queue_file_lock

    with upload_queue_file_lock(data_dir):
        child = subprocess.Popen(  # noqa: S603 - our own interpreter on a fixed script
            [sys.executable, "-c", child_code, str(data_dir), str(stems_dir)], cwd=repo_root
        )
        time.sleep(1.5)
        still_waiting = child.poll() is None
        # This holder is another pass mid-transaction: it writes its own entry.
        write_json_atomically(
            budget.upload_queue_path(data_dir), {"schema_version": 1, "bundles": {"holder": entry}}
        )
    assert child.wait(timeout=60) == 0
    assert still_waiting, "the child saved while another process held the lock"
    assert set(budget.load_upload_queue(data_dir)) == {"child", "holder"}


@pytest.mark.requirement("STEM-42")
def test_a_deck_load_that_finds_the_bundle_already_local_still_takes_the_lease(tmp_path: Path):
    """[if] a deck-load hydrate finds another hydrator already published the bundle [then] it is marked served, [else stop].

    MUTATION TARGET: return ``already_local`` without the handoff and the
    bundle is unprotected once the publishing hydrator drops its pin.
    """
    from apps.cloud import stem_hydration

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"raced": make_bundle(stems_dir, "raced", atime=100.0)}
    registry = OpenDeckRegistry()
    original = stem_hydration.OPEN_DECKS
    stem_hydration.OPEN_DECKS = registry
    try:
        deck = stem_hydration.hydrate_one(
            "raced", data_dir=data_dir, source=_CopySource(tmp_path), index=index,  # type: ignore[arg-type]
            stems_dir=stems_dir, hand_off_to_deck=True,
        )
        assert deck.status == "already_local"
        assert "raced" in registry.open_ids()
        # Control: a bulk caller finding it local takes no lease.
        fresh = OpenDeckRegistry()
        stem_hydration.OPEN_DECKS = fresh
        bulk = stem_hydration.hydrate_one(
            "raced", data_dir=data_dir, source=_CopySource(tmp_path), index=index,  # type: ignore[arg-type]
            stems_dir=stems_dir,
        )
        assert bulk.status == "already_local"
        assert "raced" not in fresh.open_ids()
    finally:
        stem_hydration.OPEN_DECKS = original


@pytest.mark.requirement("STEM-42")
def test_arming_hydration_in_the_engine_marks_it_the_deck_holder(tmp_path: Path, monkeypatch):
    """[if] the engine arms stem hydration [then] its registry is the one eviction trusts, [else stop].

    MUTATION TARGET: drop the ``holds_decks`` line in
    ``_install_armed_stem_hydration`` and the engine's own post-hydrate pass
    never frees space.
    """
    from fastapi import FastAPI

    from apps.cloud import stem_hydration
    from apps.webui.server import app_wiring

    registry = OpenDeckRegistry()
    monkeypatch.setattr(stem_hydration, "OPEN_DECKS", registry)
    assert registry.holds_decks is False, "control: a fresh process (a worker) holds no decks"

    app_wiring._install_armed_stem_hydration(
        FastAPI(), data_dir=tmp_path, source=object(), start_refresh_thread=False
    )

    assert registry.holds_decks is True
