"""Stem cache budget under concurrency and in-place re-renders (STEM-40, STEM-42).

Three blocking findings from Codex's review of #4974 (f202046b4), each with
the test that bites on the defect and a control for its overshoot:

* [if] a deck opens a bundle while an eviction pass is hashing it [then] the
  live registry is asked again before removal and the bundle stays.
* [if] two passes write the upload queue at once [then] neither loses its
  temp file to the other.
* [if] a bundle is re-rendered under the same file names [then] the next
  timer pass queues it for upload even on a healthy disk.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from apps.cloud import stem_cache_budget as budget
from apps.cloud.stem_cache_budget import GIB
from apps.cloud.stem_cache_settings import write_json_atomically
from apps.cloud.stem_hydration import OpenDeckRegistry
from tests.cloudsync.stem_cache_rig import (
    FLOOR_BYTES,
    disk_usage,
    enforce,
    make_bundle,
    wav_bytes,
)


@pytest.mark.requirement("STEM-42")
def test_a_bundle_a_deck_opens_during_the_pass_is_not_evicted(tmp_path: Path):
    """[if] a deck opens a bundle after the pass read ``protected`` [then] the live recheck keeps it, [else stop].

    MUTATION TARGET: drop the ``live_protected`` recheck in ``_evict_lru``
    and ``opened-mid-pass`` is removed.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"opened-mid-pass": make_bundle(stems_dir, "opened-mid-pass", atime=100.0)}
    registry = OpenDeckRegistry()
    snapshot = registry.open_ids()  # read before the scan, as the callers do
    registry.mark_open("opened-mid-pass")  # a deck opens it while the pass hashes

    report = enforce(
        stems_dir,
        data_dir,
        index=index,
        disk=disk_usage(free=0),
        protected=snapshot,
        live_protected=registry.open_ids,
    )

    assert report.evicted_stable_ids == ()
    assert (stems_dir / "opened-mid-pass").is_dir()


@pytest.mark.requirement("STEM-41")
def test_the_live_recheck_still_evicts_a_bundle_nobody_opened(tmp_path: Path):
    """[if] the live registry names nothing [then] eviction proceeds as before, [else stop].

    Overshoot control for the test above: a recheck that refused every
    removal would pass it and leave the volume full.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"idle": make_bundle(stems_dir, "idle", atime=100.0)}
    registry = OpenDeckRegistry()

    report = enforce(
        stems_dir,
        data_dir,
        index=index,
        disk=disk_usage(free=FLOOR_BYTES - 1),
        live_protected=registry.open_ids,
    )

    assert report.evicted_stable_ids == ("idle",)


@pytest.mark.requirement("STEM-40")
def test_concurrent_queue_writes_never_lose_each_other_s_temp_file(tmp_path: Path):
    """[if] two writers save the same JSON file at once [then] both succeed and the file parses, [else stop].

    MUTATION TARGET: go back to one shared ``<name>.tmp`` and writers raise
    FileNotFoundError when one replace consumes the other's temp file.
    """
    target = tmp_path / "queue.json"
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def writer(n: int) -> None:
        start.wait()
        try:
            for i in range(200):
                write_json_atomically(target, {"writer": n, "i": i, "pad": "x" * 4096})
        except BaseException as exc:  # noqa: BLE001 - the test reports any failure
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(json.loads(target.read_text(encoding="utf-8"))) == {"writer", "i", "pad"}
    assert [p.name for p in tmp_path.iterdir()] == ["queue.json"], "no temp file left behind"


@pytest.mark.requirement("STEM-40")
def test_a_same_name_rerender_reaches_the_upload_queue_on_a_healthy_disk(tmp_path: Path):
    """[if] a bundle is re-rendered in place with new bytes [then] the next pass queues it and status stops counting it evictable, [else stop].

    MUTATION TARGET: skip the fingerprint check in ``_refreshed_upload_queue``
    and the re-render never reaches the queue while the disk is healthy.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"rerendered": make_bundle(stems_dir, "rerendered", atime=100.0)}
    healthy = disk_usage(free=200 * GIB)

    first = enforce(stems_dir, data_dir, index=index, disk=healthy)
    assert first.queued_for_upload == ()

    vocals = stems_dir / "rerendered" / "vocals.wav"
    vocals.write_bytes(wav_bytes(seed=7))
    os.utime(vocals, ns=(vocals.stat().st_atime_ns, vocals.stat().st_mtime_ns + 1_000_000_000))

    second = enforce(stems_dir, data_dir, index=index, disk=healthy)
    assert second.queued_for_upload == ("rerendered",)
    assert second.evicted_stable_ids == ()
    state = budget.status(
        stems_dir,
        data_dir=data_dir,
        index=index,
        protected=frozenset(),
        can_rehydrate=True,
        disk=healthy,
    )
    assert state["local_only_stable_ids"] == ["rerendered"]
    assert state["evictable_bundle_count"] == 0


@pytest.mark.requirement("STEM-40")
def test_an_unchanged_bundle_is_hashed_once_not_every_pass(tmp_path: Path, monkeypatch):
    """[if] a verified bundle has not changed [then] later passes do not hash it again, [else stop].

    Overshoot control: re-hashing every bundle on every pass would also find
    the re-render above, at the cost of reading the whole cache each tick.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"steady": make_bundle(stems_dir, "steady", atime=100.0)}
    healthy = disk_usage(free=200 * GIB)
    enforce(stems_dir, data_dir, index=index, disk=healthy)

    hashed: list[str] = []
    real = budget.unconfirmed_reason

    def counting(bundle, idx):
        hashed.append(bundle.stable_id)
        return real(bundle, idx)

    monkeypatch.setattr(budget, "unconfirmed_reason", counting)
    enforce(stems_dir, data_dir, index=index, disk=healthy)
    enforce(stems_dir, data_dir, index=index, disk=healthy)
    assert hashed == []


@pytest.mark.requirement("STEM-40")
def test_the_post_hydrate_pass_hashes_nothing_to_look_for_rerenders(tmp_path: Path, monkeypatch):
    """[if] a pass is bounded by ``max_evict_bytes`` (a deck is waiting) [then] it hashes no unverified bundle for the queue, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"fresh": make_bundle(stems_dir, "fresh", atime=100.0)}
    hashed: list[str] = []
    monkeypatch.setattr(
        budget, "unconfirmed_reason", lambda bundle, idx: hashed.append(bundle.stable_id)
    )
    enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=200 * GIB), max_evict_bytes=1)
    assert hashed == []


@pytest.mark.requirement("STEM-41")
def test_a_bundle_another_pass_evicted_first_is_skipped_not_an_error(tmp_path: Path, monkeypatch):
    """[if] a concurrent pass removes the bundle this pass just confirmed [then] this pass skips it and frees nothing, without raising, [else stop].

    MUTATION TARGET: go back to a bare ``shutil.rmtree`` and the second
    remover raises FileNotFoundError, which the post-hydrate pass surfaced as
    a failed deck load (Codex on #4974, 3e9a4592b).
    """
    import shutil

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"contested": make_bundle(stems_dir, "contested", atime=100.0)}
    real = budget.unconfirmed_reason

    def confirm_then_lose_the_race(bundle, idx):
        verdict = real(bundle, idx)
        shutil.rmtree(bundle.path, ignore_errors=True)  # the other pass removes it now
        return verdict

    monkeypatch.setattr(budget, "unconfirmed_reason", confirm_then_lose_the_race)
    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert report.evicted_stable_ids == ()
    assert report.bytes_freed == 0


def test_claim_and_remove_lets_exactly_one_remover_win(tmp_path: Path):
    """[if] two passes remove the same bundle [then] one returns True and the other False, and nothing is left behind, [else stop]."""
    from apps.cloud.stem_bundles import claim_and_remove

    stems_dir = tmp_path / "stems"
    make_bundle(stems_dir, "one", atime=100.0)
    assert claim_and_remove(stems_dir / "one") is True
    assert claim_and_remove(stems_dir / "one") is False
    assert list(stems_dir.iterdir()) == []


@pytest.mark.requirement("STEM-40")
def test_an_older_pass_does_not_erase_a_bundle_a_newer_pass_queued(tmp_path: Path):
    """[if] a pass that scanned before a bundle appeared saves after a pass that queued it [then] the entry stays, [else stop].

    MUTATION TARGET: write this pass's snapshot without merging the on-disk
    file and ``late`` leaves the queue until some later pass re-queues it.
    """
    data_dir, stems_dir = tmp_path / "data", tmp_path / "stems"
    (stems_dir / "late").mkdir(parents=True)
    entry: dict[str, object] = {"reason": "not_in_index", "bytes": 1, "queued_at": "t"}
    # The newer pass saw "late" and queued it, with a verified neighbor.
    budget.save_upload_queue(
        data_dir, {"late": entry}, {"other": "fp"}, stems_dir=stems_dir, seen={"late"}
    )
    (stems_dir / "other").mkdir()
    # The older pass scanned before either existed and found nothing to queue.
    written = budget.save_upload_queue(data_dir, {}, {}, stems_dir=stems_dir, seen=set())
    assert "late" in written and "late" in budget.load_upload_queue(data_dir)
    assert budget.load_verified_fingerprints(data_dir) == {"other": "fp"}


@pytest.mark.requirement("STEM-40")
def test_the_merge_does_not_keep_what_a_pass_saw_or_what_is_gone(tmp_path: Path):
    """Overshoot control: a pass that SAW the bundle decides for it, and an
    entry whose directory is gone is dropped, so the merge cannot pin stale
    rows forever."""
    data_dir, stems_dir = tmp_path / "data", tmp_path / "stems"
    (stems_dir / "seen-clean").mkdir(parents=True)
    entry: dict[str, object] = {"reason": "not_in_index", "bytes": 1, "queued_at": "t"}
    budget.save_upload_queue(
        data_dir,
        {"seen-clean": entry, "deleted": entry},
        {},
        stems_dir=stems_dir,
        seen={"seen-clean", "deleted"},
    )
    written = budget.save_upload_queue(
        data_dir, {}, {}, stems_dir=stems_dir, seen={"seen-clean"}
    )
    assert written == {} and budget.load_upload_queue(data_dir) == {}
