"""The re-compress swap clears a landed publish's leftover swap backup first (STEM-58).

[if] a publisher landed this bundle but died before deleting its generic swap backup
[then] the pass removes that backup before its renames, [else stop].
[if] that backup cannot be removed [then] the pass stops with the bundle untouched, [else stop].

The read barrier restores a generic swap backup before it reads the re-compress
journal, so a pass killed between its renames while the older generic backup
sat beside the bundle would serve that older render and strand the newer one
in the pass's own backup (Codex on #6820). Real FLAC and MP3 bytes, the real
lock and swap; the only double is the filesystem refusing to delete a backup.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.stems import bundle_lock
from apps.stems import recompress as rc
from apps.stems.artifacts import load_stem_bundle
from apps.stems.bundle_lock import SwapBackupStuckError, swap_backup_path
from tests.stems.recompress_support import _data_dir, _flac_bundle, _snapshot, _source

pytestmark = [pytest.mark.requirement("STEM-58"), pytest.mark.requires_audio_stack]


def _leftover_generic_backup(bundle: Path) -> Path:
    """The older render a publisher killed after its move-in leaves at the swap slot."""
    backup = swap_backup_path(bundle)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle, backup)
    (backup / "manifest.json").write_text('{"older": "render"}', encoding="utf-8")
    return backup


def test_a_leftover_generic_backup_is_gone_before_the_pass_moves_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "leftover", _source(tmp_path, "a"))
    backup = _leftover_generic_backup(bundle)
    (candidate,) = rc.plan(data)
    rename = rc._rename_durably
    present_at_first_move: list[bool] = []

    def record_then_rename(source: Path, destination: Path) -> None:
        if not present_at_first_move:
            present_at_first_move.append(backup.exists())
        rename(source, destination)

    monkeypatch.setattr(rc, "_rename_durably", record_then_rename)

    outcome = rc.recompress_bundle(candidate, tmp_path / "backup")

    assert present_at_first_move == [False], "the older generic backup was still beside the bundle when it moved"
    assert outcome.after_bytes > 0
    assert load_stem_bundle(candidate.stable_id, roots=[data / "state" / "stems"]).media_type == "audio/mpeg"


def test_a_generic_backup_that_will_not_go_stops_the_pass_before_any_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "stuck", _source(tmp_path, "a"))
    backup = _leftover_generic_backup(bundle)
    live = _snapshot(bundle)
    (candidate,) = rc.plan(data)
    monkeypatch.setattr(bundle_lock, "_discard_backup", lambda _backup: None)  # a deck streams one of its parts

    with pytest.raises(SwapBackupStuckError):
        rc.recompress_bundle(candidate, tmp_path / "backup")

    assert _snapshot(bundle) == live, "the live bundle changed"
    assert backup.is_dir(), "control: the backup the filesystem refused to delete is still there"
    assert not (tmp_path / "backup").exists(), "nothing was moved into the pass's backup"


def test_control_a_pass_with_no_leftover_backup_still_swaps(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "clean", _source(tmp_path, "a"))
    (candidate,) = rc.plan(data)

    rc.recompress_bundle(candidate, tmp_path / "backup")

    assert not swap_backup_path(bundle).exists()
    assert load_stem_bundle(candidate.stable_id, roots=[data / "state" / "stems"]).media_type == "audio/mpeg"
