"""Stem cache budget under concurrency and in-place re-renders (STEM-40, STEM-42).

Three blocking findings from Codex's review of #4974 (f202046b4), each with
the test that bites on the defect and a control for its overshoot:

* [if] a deck opens a bundle while an eviction pass is hashing it [then] the
  live registry is asked again before removal and the bundle stays.
* [if] two passes write the upload queue at once [then] neither loses its
  temp file to the other.
* [if] a bundle is re-rendered under the same file names [then] the next
  timer pass queues it for upload even on a healthy disk.

And one from Codex's review of #6820 (2dff9aa9b, STEM-58):

* [if] another writer holds the bundle lock [then] eviction waits for it, and
  removes the bundle only if it is still the one it confirmed against R2; a
  bundle a re-compress swap replaced meanwhile is kept. With no lock held an
  evictable bundle is still evicted.
* [if] a landed swap left its backup [then] eviction discards it with the
  bundle, so no later read restores the older render (Codex on #6821).
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
from tests.cloudsync.stem_cache_budget_races_helpers import (
    _enforce_in_thread,
    _hold_bundle_lock,
    _lose_the_claim_for,
    _rewrite_same_size_same_mtime,
    _seen,
    _swap_in_a_new_bundle,
    _swap_in_renamed_parts,
    _swap_on_hash,
)
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
    monkeypatch.setattr(budget, "unconfirmed_reason", lambda bundle, idx: hashed.append(bundle.stable_id))
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
        data_dir, {"late": entry}, {"other": "fp"}, stems_dir=stems_dir, seen=_seen(stems_dir, "late")
    )
    (stems_dir / "other").mkdir()
    # The older pass scanned before either existed and found nothing to queue.
    written = budget.save_upload_queue(data_dir, {}, {}, stems_dir=stems_dir, seen={})
    assert "late" in written and "late" in budget.load_upload_queue(data_dir)
    assert budget.load_verified_fingerprints(data_dir) == {"other": "fp"}


@pytest.mark.requirement("STEM-40")
def test_the_merge_does_not_keep_what_a_pass_saw_or_what_is_gone(tmp_path: Path):
    """[if] the pass saw the bundle, or its directory is gone [then] the merge drops the entry, [else stop].

    Overshoot control: the merge cannot pin stale rows forever."""
    data_dir, stems_dir = tmp_path / "data", tmp_path / "stems"
    (stems_dir / "seen-clean").mkdir(parents=True)
    entry: dict[str, object] = {"reason": "not_in_index", "bytes": 1, "queued_at": "t"}
    budget.save_upload_queue(
        data_dir,
        {"seen-clean": entry, "deleted": entry},
        {},
        stems_dir=stems_dir,
        seen={**_seen(stems_dir, "seen-clean"), "deleted": "gone"},
    )
    written = budget.save_upload_queue(data_dir, {}, {}, stems_dir=stems_dir, seen=_seen(stems_dir, "seen-clean"))
    assert written == {} and budget.load_upload_queue(data_dir) == {}


@pytest.mark.requirement("STEM-41")
def test_a_lost_claim_counts_toward_the_shortfall(tmp_path: Path, monkeypatch):
    """[if] two passes need one bundle and the other wins the claim on it [then] this pass evicts nothing more, [else stop].

    MUTATION TARGET: credit nothing for a lost claim and this pass walks on
    and evicts ``next``, freeing twice what the disk needed.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"lru": make_bundle(stems_dir, "lru", atime=100.0)}
    index["next"] = make_bundle(stems_dir, "next", atime=200.0)
    _lose_the_claim_for(budget, "lru", monkeypatch)
    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))
    assert report.evicted_stable_ids == () and report.blocked_reason is None
    assert (stems_dir / "next").is_dir()


@pytest.mark.requirement("STEM-41")
def test_a_lost_claim_smaller_than_the_shortfall_still_evicts_the_rest(tmp_path: Path, monkeypatch):
    """[if] the bundle another pass took is not enough [then] this pass evicts the next one, [else stop].

    Overshoot control: crediting a lost claim must not stop a pass that
    still has a shortfall.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"lru": make_bundle(stems_dir, "lru", atime=100.0)}
    index["next"] = make_bundle(stems_dir, "next", atime=200.0)
    one_bundle = sum(p.stat().st_size for p in (stems_dir / "lru").rglob("*") if p.is_file())
    _lose_the_claim_for(budget, "lru", monkeypatch)
    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - one_bundle - 1))
    assert report.evicted_stable_ids == ("next",)


@pytest.mark.requirement("STEM-41")
def test_a_bundle_removed_before_it_was_hashed_counts_toward_the_shortfall(tmp_path: Path, monkeypatch):
    """[if] another pass removes the LRU bundle before this pass hashes it [then] this pass evicts nothing more, [else stop].

    The early window of the lost-claim race. MUTATION TARGET: skip a
    ``REASON_GONE`` bundle without credit and ``next`` is evicted too.
    """
    import shutil

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"lru": make_bundle(stems_dir, "lru", atime=100.0)}
    index["next"] = make_bundle(stems_dir, "next", atime=200.0)
    real = budget.unconfirmed_reason

    def removed_first(bundle, idx):
        if bundle.stable_id == "lru":
            shutil.rmtree(bundle.path, ignore_errors=True)
        return real(bundle, idx)

    monkeypatch.setattr(budget, "unconfirmed_reason", removed_first)
    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))
    assert report.evicted_stable_ids == () and (stems_dir / "next").is_dir()


@pytest.mark.requirement("STEM-41")
@pytest.mark.parametrize("call", [1, 2], ids=["before-the-scan-hash", "during-the-claim-hash"])
def test_a_bundle_swapped_for_other_file_names_frees_nothing_and_is_kept(tmp_path: Path, monkeypatch, call):
    """[if] the LRU bundle is swapped for one with other file names while hashed [then] it is kept and the next one is evicted, [else stop].

    A missing file reads as REASON_GONE, which used to credit the bundle's
    bytes as freed by another pass, so the pass stopped short of its target
    with the replacement still on disk (Codex on #6820). MUTATION TARGETS:
    credit REASON_GONE in ``_evict_lru`` again (call 1), or return False on
    it in ``claim_if_unchanged`` without looking under the lock (call 2),
    and ``next`` is kept.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"lru": make_bundle(stems_dir, "lru", atime=100.0)}
    index["next"] = make_bundle(stems_dir, "next", atime=200.0)
    _swap_on_hash(monkeypatch, "lru", call, _swap_in_renamed_parts)

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert report.evicted_stable_ids == ("next",)
    assert sorted(p.name for p in (stems_dir / "lru").iterdir() if p.suffix != ".json") == [
        "bass.mp3",
        "drums.wav",
        "other.wav",
        "vocals.wav",
    ]
    assert not (stems_dir / "next").exists()


@pytest.mark.requirement("STEM-41")
def test_a_bundle_removed_while_the_claim_hashes_it_still_counts_toward_the_shortfall(tmp_path: Path, monkeypatch):
    """[if] another pass removes the LRU bundle during this pass's claim hash [then] this pass evicts nothing more, [else stop].

    Overshoot control for the test above: a claim that kept crediting
    nothing for a missing file would walk on and evict ``next`` too.
    """
    import shutil

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"lru": make_bundle(stems_dir, "lru", atime=100.0)}
    index["next"] = make_bundle(stems_dir, "next", atime=200.0)
    _swap_on_hash(monkeypatch, "lru", 2, shutil.rmtree)

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert report.evicted_stable_ids == () and (stems_dir / "next").is_dir()


@pytest.mark.requirement("STEM-41")
def test_a_bundle_evicted_mid_scan_is_skipped_not_an_error(tmp_path: Path, monkeypatch):
    """[if] another pass removes a bundle between listing a file and stat-ing it [then] the scan skips that bundle and carries on, [else stop].

    MUTATION TARGET: drop the FileNotFoundError guard in ``_scan_one`` and
    the post-hydrate pass raises, failing a deck load that had succeeded.
    """
    import shutil

    from apps.cloud import stem_bundles

    stems_dir = tmp_path / "stems"
    make_bundle(stems_dir, "vanishing", atime=100.0)
    make_bundle(stems_dir, "stays", atime=200.0)
    real_stat = Path.stat

    def stat_after_removal(self: Path, *args, **kwargs):
        if self.parent.name == "vanishing" and (stems_dir / "vanishing").exists():
            shutil.rmtree(stems_dir / "vanishing")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_after_removal)
    assert [b.stable_id for b in stem_bundles.scan_bundles(stems_dir)] == ["stays"]


@pytest.mark.requirement("STEM-41")
def test_a_claim_a_failed_rmtree_left_behind_is_swept_by_the_next_pass(tmp_path: Path):
    """[if] a pass renamed a bundle to ``.evicting-`` but could not delete it [then] the next enforcement pass removes it, [else stop].

    Scans skip that name, so without the sweep its bytes would never be
    counted or freed again. MUTATION TARGET: drop the sweep in ``enforce``.
    """
    from apps.cloud.stem_bundles import EVICTING_MARKER

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    make_bundle(stems_dir, "abandoned", atime=100.0)
    abandoned = stems_dir / f"abandoned{EVICTING_MARKER}deadbeef"
    os.rename(stems_dir / "abandoned", abandoned)
    enforce(stems_dir, data_dir, index={}, disk=disk_usage(free=200 * GIB))
    assert not abandoned.exists()
    # Control: a dry run writes nothing, so it leaves the claim alone.
    make_bundle(stems_dir, "again", atime=100.0)
    os.rename(stems_dir / "again", abandoned)
    enforce(stems_dir, data_dir, index={}, disk=disk_usage(free=200 * GIB), dry_run=True)
    assert abandoned.exists()


@pytest.mark.requirement("STEM-40")
def test_a_pass_that_scanned_an_older_render_keeps_the_newer_verdict(tmp_path: Path):
    """[if] a bundle is re-rendered after a pass scanned it and a newer pass queued the new bytes [then] the older pass's save keeps that entry, [else stop].

    MUTATION TARGET: let every seen id take this pass's verdict again and the
    re-render is reported as R2-backed (Codex, PR #4974).
    """
    data_dir, stems_dir = tmp_path / "data", tmp_path / "stems"
    make_bundle(stems_dir, "rerendered", atime=100.0)
    older = _seen(stems_dir, "rerendered")  # pass A scans generation 1
    (stems_dir / "rerendered" / "vocals.wav").write_bytes(b"generation 2, a different length")
    entry: dict[str, object] = {"reason": "content_differs_from_r2_index", "bytes": 1, "queued_at": "t"}
    budget.save_upload_queue(  # pass B judged generation 2
        data_dir, {"rerendered": entry}, {}, stems_dir=stems_dir, seen=_seen(stems_dir, "rerendered")
    )
    # Pass A judged generation 1 clean and saves last.
    written = budget.save_upload_queue(data_dir, {}, {"rerendered": "gen1-fp"}, stems_dir=stems_dir, seen=older)
    assert written == {"rerendered": entry}
    assert budget.load_verified_fingerprints(data_dir) == {}


@pytest.mark.requirement("STEM-40")
def test_a_pass_whose_scan_is_current_still_overrides_the_file(tmp_path: Path):
    """[if] the bundle is unchanged since this pass scanned it [then] this pass's verdict replaces the on-disk one, [else stop].

    Overshoot control: the on-disk entry must not win for every seen id.
    """
    data_dir, stems_dir = tmp_path / "data", tmp_path / "stems"
    make_bundle(stems_dir, "settled", atime=100.0)
    entry: dict[str, object] = {"reason": "not_in_r2_index", "bytes": 1, "queued_at": "t"}
    budget.save_upload_queue(data_dir, {"settled": entry}, {}, stems_dir=stems_dir, seen=_seen(stems_dir, "settled"))
    written = budget.save_upload_queue(
        data_dir, {}, {"settled": "fp"}, stems_dir=stems_dir, seen=_seen(stems_dir, "settled")
    )
    assert written == {}
    assert budget.load_verified_fingerprints(data_dir) == {"settled": "fp"}


@pytest.mark.requirement("STEM-40")
def test_eviction_waits_for_the_bundle_lock_and_keeps_a_bundle_swapped_meanwhile(tmp_path: Path):
    """[if] the bundle is replaced while eviction waits for its lock [then] the new bundle is kept, [else stop].

    MUTATION TARGET: claim without the lock, or without re-taking the
    fingerprint inside it, and the newly swapped-in bundle is deleted.
    """
    import time

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"swapped": make_bundle(stems_dir, "swapped", atime=100.0)}
    with _hold_bundle_lock(data_dir, "swapped"):
        thread, reports = _enforce_in_thread(stems_dir, data_dir, index)
        time.sleep(1.0)  # an unlocked pass over one small bundle takes milliseconds
        assert thread.is_alive() and (stems_dir / "swapped").is_dir(), "eviction did not wait for the lock"
        swapped_in = _swap_in_a_new_bundle(stems_dir, "swapped")
    thread.join(timeout=60)

    assert not thread.is_alive()
    assert reports[0].evicted_stable_ids == () and reports[0].bytes_freed == 0
    assert {p.name: p.read_bytes() for p in (stems_dir / "swapped").iterdir()} == swapped_in


@pytest.mark.requirement("STEM-41")
def test_eviction_that_waited_for_the_lock_still_evicts_an_unchanged_bundle(tmp_path: Path):
    """[if] the lock holder changed nothing [then] the bundle is evicted once it is released, [else stop].

    Overshoot control: the re-check cannot keep every bundle that was locked.
    """
    import time

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"steady": make_bundle(stems_dir, "steady", atime=100.0)}
    with _hold_bundle_lock(data_dir, "steady"):
        thread, reports = _enforce_in_thread(stems_dir, data_dir, index)
        time.sleep(1.0)
        assert thread.is_alive() and (stems_dir / "steady").is_dir(), "eviction did not wait for the lock"
    thread.join(timeout=60)

    assert reports[0].evicted_stable_ids == ("steady",)
    assert not (stems_dir / "steady").exists()


@pytest.mark.requirement("STEM-41")
def test_an_evictable_bundle_with_no_lock_held_is_evicted(tmp_path: Path):
    """[if] nobody holds the bundle lock [then] the confirmed LRU bundle is evicted at once, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"free": make_bundle(stems_dir, "free", atime=100.0)}

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert report.evicted_stable_ids == ("free",)
    assert not (stems_dir / "free").exists()


@pytest.mark.requirement("STEM-40")
def test_a_same_size_rewrite_the_clock_did_not_see_is_kept(tmp_path: Path):
    """[if] a bundle's bytes change under the lock while names, sizes and mtimes do not [then] the in-lock R2 hash keeps it, [else stop].

    Codex P1 on #6821.

    MUTATION TARGET: claim on the metadata fingerprint alone and this new,
    not-yet-uploaded render is deleted.
    """
    import time

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"coarse": make_bundle(stems_dir, "coarse", atime=100.0)}
    with _hold_bundle_lock(data_dir, "coarse"):
        thread, reports = _enforce_in_thread(stems_dir, data_dir, index)
        time.sleep(1.0)
        assert thread.is_alive() and (stems_dir / "coarse").is_dir(), "eviction did not wait for the lock"
        rewritten = _rewrite_same_size_same_mtime(stems_dir / "coarse")
    thread.join(timeout=60)

    assert not thread.is_alive()
    assert reports[0].evicted_stable_ids == () and reports[0].bytes_freed == 0
    assert {p.name: p.read_bytes() for p in (stems_dir / "coarse").iterdir()} == rewritten
