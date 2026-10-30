"""The re-compress snapshot holds the bundle lock only to pin a generation (STEM-58).

[if] the volume refuses hard links [then] the snapshot copy runs with the bundle lock free, [else stop].
[if] a writer publishes the bundle after that copy [then] the swap refuses and keeps it, [else stop].

When the stem root's volume takes hard links, the snapshot is the live bundle's own inodes, linked
under the lock, and no stem byte is copied there. When it refuses them, the copy runs after the
lock is released, so a hydrate or publish of an id on the same stripe never waits it out.

Real FLAC and MP3 bytes, the real lock and the real swap; the only double is ``os.link`` refusing,
which stands in for a FAT or exFAT volume.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from apps.stems import bundle_publish
from apps.stems import recompress as rc
from apps.stems.artifacts import load_stem_bundle
from apps.stems.bundle_lock import bundle_lock
from tests.stems.recompress_support import (
    _data_dir,
    _flac_bundle,
    _fresh_parts,
    _publish_result,
    _snapshot,
    _source,
)

pytestmark = [pytest.mark.requirement("STEM-58"), pytest.mark.requires_audio_stack]


def _lock_is_free(bundle_dir: Path) -> bool:
    """True when another holder can take ``bundle_dir``'s lock within a few seconds."""
    taken = threading.Event()

    def take() -> None:
        with bundle_lock(bundle_dir):
            taken.set()

    probe = threading.Thread(target=take, daemon=True)
    probe.start()
    free = taken.wait(timeout=5)
    probe.join(timeout=60)
    return free


def _refuse_hard_links(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_links(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(rc.os, "link", no_links)


def test_the_snapshot_is_the_live_generation_linked_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "linked", _source(tmp_path, "a"))
    live = {path.name: path.stat().st_ino for path in bundle.iterdir()}
    (candidate,) = rc.plan(data)
    copies: list[Path] = []
    monkeypatch.setattr(rc, "_copy_unlocked", lambda src, _dst: copies.append(src))
    seen: dict[str, int] = {}

    def look_at_the_snapshot() -> None:
        (snapshot,) = rc.work_parent(data / "state" / "stems").glob(".recompress-linked-*/snapshot/linked")
        seen.update({path.name: path.stat().st_ino for path in snapshot.iterdir()})

    outcome = rc.recompress_bundle(candidate, tmp_path / "backup", before_swap=look_at_the_snapshot)

    assert outcome.after_bytes > 0
    assert load_stem_bundle(candidate.stable_id, roots=[data / "state" / "stems"]).media_type == "audio/mpeg"
    assert copies == [], "a volume that takes hard links copied the snapshot"
    assert seen == live, "the snapshot is not the live bundle's own files"


def test_a_volume_without_hard_links_copies_the_snapshot_outside_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "nolinks", _source(tmp_path, "a"))
    old = _snapshot(bundle)
    (candidate,) = rc.plan(data)
    _refuse_hard_links(monkeypatch)
    copy = rc._copy_unlocked
    free_during_copy: list[bool] = []

    def copy_and_probe(src: Path, dst: Path) -> None:
        free_during_copy.append(_lock_is_free(src))
        copy(src, dst)

    monkeypatch.setattr(rc, "_copy_unlocked", copy_and_probe)

    outcome = rc.recompress_bundle(candidate, tmp_path / "backup")

    assert free_during_copy == [True], "the snapshot copy ran while the bundle lock was held"
    assert outcome.after_bytes > 0
    assert load_stem_bundle(candidate.stable_id, roots=[data / "state" / "stems"]).media_type == "audio/mpeg"
    assert _snapshot(tmp_path / "backup" / "stems" / "nolinks") == old


def test_control_the_lock_probe_sees_a_held_lock(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "held", _source(tmp_path, "a"))

    with bundle_lock(bundle):
        assert not _lock_is_free(bundle)
    assert _lock_is_free(bundle)


def test_a_publish_after_the_unlocked_copy_is_not_clobbered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "racing", _source(tmp_path, "a"))
    (candidate,) = rc.plan(data)
    fresh = _publish_result(_fresh_parts(tmp_path, 3))
    _refuse_hard_links(monkeypatch)
    copy = rc._copy_unlocked
    published: list[dict[str, bytes]] = []

    def copy_then_a_stems_job_publishes(src: Path, dst: Path) -> None:
        copy(src, dst)
        monkeypatch.undo()  # the publisher's own volume takes links again
        bundle_publish.publish_stems_local(
            "racing", Path("/music/a.mp3"), fresh, bundle_publish.PRESETS["hdemucs_mmi-ov0.25"], data
        )
        published.append(_snapshot(bundle))

    monkeypatch.setattr(rc, "_copy_unlocked", copy_then_a_stems_job_publishes)

    with pytest.raises(rc.BundleChangedError, match="rewritten by another writer"):
        rc.recompress_bundle(candidate, tmp_path / "backup")

    assert published, "the publisher ran"
    assert _snapshot(bundle) == published[0], "the published bundle was clobbered"
    assert not (tmp_path / "backup").exists(), "nothing was moved into the backup"
    assert os.listdir(data / "state" / "stems") == ["racing"], "no work directory left"
