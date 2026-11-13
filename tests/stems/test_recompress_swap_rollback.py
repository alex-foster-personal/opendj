"""Stem bundle swaps under recompression and republish, rollback and readers (STEM-58).

Split from ``test_recompress.py``: where a swap keeps the old bundle, how each
rename and journal step reaches the disk, how a failed swap is undone, and how
readers are held off. Every test runs the real swap code on real bundles.

[if] a swap raises after either rename [then] the old bundle is back at its path, every step was
  flushed before the next, and a landed swap whose journal will not clear still reports success,
  [else stop].
[if] two data dirs' stem roots link to sibling directories on one volume [then] their swap and
  default backups never share a directory, [else stop].
[if] a reader holds the read barrier [then] neither swap rename lands until it lets go, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.stems import recompress as rc
from tests.stems.recompress_support import (
    _data_dir,
    _flac_bundle,
    _fresh_parts,
    _publish_result,
    _source,
    _the_two_flac_publishers,
)

pytestmark = [pytest.mark.requirement("STEM-58"), pytest.mark.requires_audio_stack]


def _swap_state(tmp_path: Path, *, moved_aside: bool, new_landed: bool) -> tuple[Path, Path, Path]:
    """A swap stopped after its renames got as far as asked: old bundle holds ``old``."""
    dest, backup, new_dir = tmp_path / "stems" / "sid", tmp_path / "swap" / "sid", tmp_path / "new" / "sid"
    for directory in (dest.parent, backup.parent, new_dir.parent):
        directory.mkdir(parents=True)
    old = backup if moved_aside else dest
    old.mkdir()
    (old / "part.wav").write_bytes(b"old")
    fresh = dest if new_landed else new_dir
    fresh.mkdir()
    (fresh / "part.wav").write_bytes(b"new")
    return dest, backup, new_dir


@pytest.mark.parametrize(
    ("moved_aside", "new_landed"),
    [(False, False), (True, False), (True, True)],
    ids=["nothing-moved", "moved-aside-then-flush-failed", "new-landed-then-flush-failed"],
)
def test_a_failed_swap_is_undone_from_what_landed(tmp_path: Path, moved_aside: bool, new_landed: bool) -> None:
    """[if] a swap raised after any of its renames [then] the old bundle is back at its path and the new one at its own, [else stop].

    A directory flush can raise after its rename landed, so the move aside,
    or the move in, may have happened although the call failed; rolling back
    only around the second rename left the bundle path empty, or tried to
    move the backup onto the landed new bundle (Sol on #6820). The states
    are real directories on disk, the ones each stopped swap leaves.

    MUTATION TARGETS: return early when ``dest`` exists; skip moving the
    landed new bundle back.
    """
    from apps.stems.bundle_lock import undo_failed_swap

    dest, backup, new_dir = _swap_state(tmp_path, moved_aside=moved_aside, new_landed=new_landed)

    undo_failed_swap(dest, backup, new_dir)

    assert (dest / "part.wav").read_bytes() == b"old"
    assert (new_dir / "part.wav").read_bytes() == b"new"
    assert not backup.exists()


def test_a_rollback_whose_flushes_keep_failing_still_puts_the_old_bundle_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] every directory flush raises during a rollback [then] both renames still land and the first flush error is raised, [else stop].

    The rollback flushed after each rename, so a flush fault that outlived the
    failed swap moved the new bundle back out and raised before the old one
    came back, leaving the bundle path empty (Codex on #6820). The flush is the
    OS dependency here, so it is the one thing doubled; the renames are real.
    Control: with no fault the same state rolls back with nothing raised.

    MUTATION TARGET: flush after each rename instead of after all of them.
    """
    from apps.stems import bundle_lock

    dest, backup, new_dir = _swap_state(tmp_path / "control", moved_aside=True, new_landed=True)
    bundle_lock.undo_failed_swap(dest, backup, new_dir)
    assert (dest / "part.wav").read_bytes() == b"old"

    dest, backup, new_dir = _swap_state(tmp_path / "fault", moved_aside=True, new_landed=True)
    flushed: list[Path] = []

    def failing_flush(directory: Path) -> None:
        flushed.append(directory)
        raise OSError(5, "I/O error", str(directory))

    monkeypatch.setattr(bundle_lock, "fsync_dir", failing_flush)

    with pytest.raises(OSError, match="I/O error"):
        bundle_lock.undo_failed_swap(dest, backup, new_dir)

    assert (dest / "part.wav").read_bytes() == b"old"
    assert (new_dir / "part.wav").read_bytes() == b"new"
    assert not backup.exists()
    assert set(flushed) == {dest.parent, backup.parent, new_dir.parent}


@pytest.mark.parametrize("new_landed", [False, True], ids=["move-in-failed", "new-landed-then-flush-failed"])
def test_a_failed_first_publish_leaves_no_bundle_at_its_path(tmp_path: Path, new_landed: bool) -> None:
    """[if] a swap with no old bundle raised [then] nothing is left at the bundle path and the new one is at its own, [else stop].

    The flush after the move in can raise once the new bundle is already at
    ``dest`` (Codex on #6820 and #6821), and the publish then reports a
    failure, so a bundle left serving there would be one nothing
    acknowledged. Control: a move in that never landed is left alone.

    MUTATION TARGET: return early when ``replacing`` is false.
    """
    from apps.stems.bundle_lock import undo_failed_swap

    dest, backup, new_dir = tmp_path / "stems" / "sid", tmp_path / "swap" / "sid", tmp_path / "new" / "sid"
    for directory in (dest.parent, new_dir.parent):
        directory.mkdir(parents=True)
    fresh = dest if new_landed else new_dir
    fresh.mkdir()
    (fresh / "part.wav").write_bytes(b"new")

    undo_failed_swap(dest, backup, new_dir, replacing=False)

    assert not dest.exists()
    assert (new_dir / "part.wav").read_bytes() == b"new"
    assert not backup.exists()


def test_the_flush_after_the_move_in_is_inside_the_rollback() -> None:
    """[if] replace_bundle_dir moves the new bundle in [then] the flush of its parent that follows runs inside the try that undoes it, [else stop].

    That flush sat after the try, so a flush that raised reported a failed
    publish with the new bundle live and the old one in the backup (Codex on
    #6820 and #6821). Read from the real source, as the test below does.

    MUTATION TARGET: move the last ``fsync_dir(dest.parent)`` back out of the ``try``.
    """
    import ast
    import inspect
    import textwrap

    from apps.stems import bundle_lock

    tree = ast.parse(textwrap.dedent(inspect.getsource(bundle_lock.replace_bundle_dir)))
    (guarded,) = [node for node in ast.walk(tree) if isinstance(node, ast.Try)]
    body = [ast.unparse(stmt) for stmt in guarded.body]
    move_in = next(i for i, stmt in enumerate(body) if "new_dir.rename(dest)" in stmt)
    assert "fsync_dir(dest.parent)" in body[move_in + 1 :], body
    handler = ast.unparse(guarded.handlers[0])
    assert "replacing=had_old" in handler, handler


def test_every_post_rename_step_of_a_swap_is_inside_its_rollback() -> None:
    """[if] replace_bundle_dir or the recompress swap moves a bundle [then] every rename and flush runs inside the try that undoes it, [else stop].

    The move aside and its flushes sat before the try, so a flush that
    raised after the move left the path empty (Sol on #6820). A failing
    flush cannot be staged without replacing production code, so the
    structure is read from the real source, as the flush-order tests here
    do; ``test_a_failed_swap_is_undone_from_what_landed`` runs the undo
    itself on disk.

    MUTATION TARGET: move ``dest.rename(backup)`` or the first
    ``_rename_durably`` back out of the ``try``.
    """
    import ast
    import inspect
    import textwrap

    from apps.stems import bundle_lock

    for function, moves in (
        (bundle_lock.replace_bundle_dir, {"dest.rename(backup)", "fsync_dir(backup.parent)", "new_dir.rename(dest)"}),
        (rc._swap_in, {"_rename_durably(bundle_dir, backup_dir)", "_rename_durably(new_dir, bundle_dir)"}),
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        guarded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            and any("undo_failed_swap" in ast.unparse(handler) for handler in node.handlers)
        ]
        assert len(guarded) == 1, f"{function.__name__} has no try that undoes a failed swap"
        inside = {ast.unparse(n) for stmt in guarded[0].body for n in ast.walk(stmt) if isinstance(n, ast.Call)}
        assert moves <= inside, f"{function.__name__}: outside the rollback: {sorted(moves - inside)}"


def test_a_staging_directory_that_cannot_be_removed_is_reported_by_name(tmp_path: Path) -> None:
    """[if] a failed publish leaves staging that cannot be removed [then] ``StagingCleanupError`` names it and keeps the cause, [else stop].

    ``ignore_errors`` left a whole failed bundle under ``.stem-publish`` with
    nothing to say so (Sol on #6820). The write leaves a file where the
    staging directory was, a real ``rmtree`` failure for every user, root
    included, then fails the way a write does. Control: a publish that
    fails with a removable staging directory raises its own error and
    leaves nothing behind.

    MUTATION TARGET: ``shutil.rmtree(staging, ignore_errors=True)`` (the
    pre-fix cleanup).
    """
    from apps.stems.bundle_lock import PUBLISH_DIR_NAME, StagingCleanupError, publish_bundle

    bundle = tmp_path / "data" / "state" / "stems" / "sid"
    staging_root = tmp_path / "data" / "state" / PUBLISH_DIR_NAME

    def failing_write(staging: Path) -> None:
        (staging / "vocals.wav").write_bytes(b"half")
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        publish_bundle(bundle, failing_write, lambda entry: True)
    assert list(staging_root.iterdir()) == [], "control: a removable staging directory is removed"

    def stuck_write(staging: Path) -> None:
        staging.rmdir()
        staging.write_bytes(b"not a directory")
        raise OSError("disk full")

    with pytest.raises(StagingCleanupError) as raised:
        publish_bundle(bundle, stuck_write, lambda entry: True)
    [stuck] = list(staging_root.iterdir())
    assert str(stuck) in str(raised.value)
    chain: list[BaseException] = []
    error: BaseException | None = raised.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert any("disk full" in str(e) for e in chain), "the publish's own error is kept"


@pytest.mark.parametrize("left", ["orphaned backup", "stale backup beside a live bundle"])
def test_a_publish_carries_over_the_files_of_a_bundle_an_interrupted_swap_left_aside(tmp_path: Path, left: str) -> None:
    """[if] a writer died between its two renames, leaving only the backup [then] a publish carries that bundle's files over, [else stop].

    ``replace_bundle_dir`` restored the backup only after the carry-over had
    already read the bundle as absent, so a ``waveform.json`` beside the old
    parts was lost with the backup (Codex on #6820). Control: a stale backup
    beside a live bundle is a landed swap's leftover, so its files are not
    carried and the live bundle's are.

    MUTATION TARGETS: drop the restore before the carry-over, and the
    orphaned bundle's file is lost; restore whenever a backup exists, and the
    stale backup's file comes back over the live bundle's.
    """
    import os

    from apps.stems.bundle_lock import publish_bundle, swap_backup_path

    bundle = tmp_path / "data" / "state" / "stems" / "sid"
    bundle.mkdir(parents=True)
    (bundle / "vocals.mp3").write_bytes(b"old part")
    (bundle / "manifest.json").write_text("{}")
    (bundle / "waveform.json").write_text("old")
    backup = swap_backup_path(bundle)
    backup.parent.mkdir(parents=True)
    os.rename(bundle, backup)
    if left == "stale backup beside a live bundle":
        bundle.mkdir()
        (bundle / "vocals.mp3").write_bytes(b"live part")
        (bundle / "manifest.json").write_text("{}")
        (bundle / "pin.json").write_text("live")

    def write(staging: Path) -> None:
        (staging / "vocals.flac").write_bytes(b"new part")
        (staging / "manifest.json").write_text('{"new": true}')

    def replaced(entry: Path) -> bool:
        return entry.name == "manifest.json" or entry.stem == "vocals"

    publish_bundle(bundle, write, replaced)

    names = sorted(p.name for p in bundle.iterdir())
    if left == "orphaned backup":
        assert names == ["manifest.json", "vocals.flac", "waveform.json"]
        assert (bundle / "waveform.json").read_text() == "old"
    else:
        assert names == ["manifest.json", "pin.json", "vocals.flac"]
    assert not backup.exists()


def test_control_fsync_tree_leaves_the_bundle_as_it_was(tmp_path: Path) -> None:
    """[if] a nested bundle is flushed [then] its files and bytes are unchanged, [else stop]."""
    (tmp_path / "sid" / "sub").mkdir(parents=True)
    (tmp_path / "sid" / "vocals.mp3").write_bytes(b"v")
    (tmp_path / "sid" / "sub" / "x.json").write_text("{}")
    before = rc.bundle_fingerprint(tmp_path / "sid")
    rc._fsync_tree(tmp_path / "sid")
    assert rc.bundle_fingerprint(tmp_path / "sid") == before


def test_the_swap_renames_wait_for_the_read_barrier_so_no_reader_sees_the_gap(tmp_path: Path) -> None:
    """[if] a reader holds the eviction-claim lock [then] the swap does not empty the bundle path until it lets go, [else stop].

    The stems routes wait on that lock, not the bundle lock, before they
    read; without it a read between the two renames finds no bundle (Codex on
    #6821). Real lock files and directories, no stand-ins.

    MUTATION TARGET: drop the ``eviction_claim_lock_path`` hold in ``_swap_in``.
    """
    import threading
    import time

    from apps.cloud.stem_cache_budget import eviction_claim_lock_path
    from apps.cloud.stem_upload_queue import exclusive_file_lock

    data_dir = tmp_path / "data"
    bundle = data_dir / "state" / "stems" / "sid"
    bundle.mkdir(parents=True)
    (bundle / "vocals.flac").write_bytes(b"old")
    new_dir = tmp_path / "work" / "sid"
    new_dir.mkdir(parents=True)
    (new_dir / "vocals.mp3").write_bytes(b"new")
    backup = tmp_path / "backup" / "sid"
    fingerprint = rc.bundle_fingerprint(bundle)
    errors: list[BaseException] = []

    def swap() -> None:
        try:
            rc._swap_in(new_dir, bundle, backup, fingerprint, None, lambda: None)
        except Exception as exc:  # noqa: BLE001 - reported to the main thread below
            errors.append(exc)

    worker = threading.Thread(target=swap)
    with exclusive_file_lock(eviction_claim_lock_path(data_dir, "sid")):
        worker.start()
        time.sleep(1.0)  # an unblocked swap takes milliseconds
        assert worker.is_alive(), "the swap moved the bundle while a reader held the barrier"
        assert (bundle / "vocals.flac").read_bytes() == b"old", "the bundle path was emptied under the barrier"
    worker.join(timeout=60)
    assert not errors, errors
    # Control: once released, the swap lands whole.
    assert (bundle / "vocals.mp3").read_bytes() == b"new"
    assert (backup / "vocals.flac").read_bytes() == b"old"


def test_control_rename_durably_moves_the_bundle_and_remove_journal_drops_the_entry(tmp_path: Path) -> None:
    """[if] the durable helpers run [then] the bundle is moved whole and the entry is gone, [else stop]."""
    (tmp_path / "a" / "sid").mkdir(parents=True)
    (tmp_path / "a" / "sid" / "vocals.mp3").write_bytes(b"x")
    (tmp_path / "b").mkdir()
    rc._rename_durably(tmp_path / "a" / "sid", tmp_path / "b" / "sid")
    assert not (tmp_path / "a" / "sid").exists()
    assert (tmp_path / "b" / "sid" / "vocals.mp3").read_bytes() == b"x"
    entry = tmp_path / "b" / "sid.json"
    entry.write_text("{}")
    rc._remove_journal(entry)
    assert not entry.exists()


def test_control_mkdir_durably_makes_missing_levels_and_keeps_existing_ones(tmp_path: Path) -> None:
    """[if] some levels exist and some do not [then] the missing ones are created and the existing ones keep their contents, [else stop]."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "keep.txt").write_text("kept")
    rc._mkdir_durably(tmp_path / "a" / "b" / "c")
    assert (tmp_path / "a" / "b" / "c").is_dir()
    assert (tmp_path / "a" / "keep.txt").read_text() == "kept"
    rc._mkdir_durably(tmp_path / "a" / "b" / "c")  # idempotent


def test_control_the_swap_journal_written_durably_still_reads_back(tmp_path: Path) -> None:
    """[if] the journal is written [then] it names the bundle and backup and leaves no temp file, [else stop]."""
    bundle = tmp_path / "data" / "state" / "stems" / "sid"
    bundle.mkdir(parents=True)
    journal = rc._write_swap_journal(bundle, tmp_path / "backup" / "sid")
    assert json.loads(journal.read_text()) == {
        "bundle": str(bundle.resolve()),
        "backup": str((tmp_path / "backup" / "sid").resolve()),
    }
    assert sorted(p.name for p in journal.parent.iterdir()) == ["sid.json"]


@pytest.mark.parametrize("publisher", ["bundle_publish", "modal_vocal_farm"])
def test_a_republish_never_rewrites_a_part_a_reader_has_open(tmp_path: Path, publisher: str) -> None:
    """[if] a deck has a part open while a farm republishes that bundle [then] it reads the whole old part, [else stop].

    The web routes do not take the bundle lock, so a publish that rewrote
    each part in place would truncate the file a deck is streaming (Codex P1
    on #6821). A real open file and real parts, no stand-ins.

    MUTATION TARGET: write the parts straight into the live bundle directory.
    """
    from apps.stems import bundle_publish

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "streamed", _source(tmp_path, "a"))
    old = (bundle / "vocals.flac").read_bytes()
    fresh = _publish_result(_fresh_parts(tmp_path, 9))
    assert fresh["stems"]["vocals"] != old, "control: the republish carries other bytes"

    with (bundle / "vocals.flac").open("rb") as streaming:
        _the_two_flac_publishers()[publisher](
            "streamed", Path("/music/a.mp3"), fresh, bundle_publish.PRESETS["hdemucs_mmi-ov0.25"], data
        )
        assert streaming.read() == old, "the open part was rewritten under the reader"
    assert (bundle / "vocals.flac").read_bytes() == fresh["stems"]["vocals"], "control: the new part landed"
    assert not list((data / "state" / ".stem-publish").iterdir()), "no staging directory left behind"


@pytest.mark.parametrize("publisher", ["bundle_publish", "modal_vocal_farm"])
def test_a_republish_carries_over_files_that_are_not_stem_parts(tmp_path: Path, publisher: str) -> None:
    """[if] a bundle holds a file that is neither a part nor the manifest [then] the republish keeps it, [else stop].

    MUTATION TARGET: treat every entry as replaced, and a republish drops
    files another writer keeps in the bundle directory.
    """
    from apps.stems import bundle_publish

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "kept", _source(tmp_path, "a"))
    (bundle / "waveform.json").write_text('{"peaks": [1, 2]}\n', encoding="utf-8")
    (bundle / "extras").mkdir()
    (bundle / "extras" / "note.txt").write_text("hi\n", encoding="utf-8")

    _the_two_flac_publishers()[publisher](
        "kept",
        Path("/music/a.mp3"),
        _publish_result(_fresh_parts(tmp_path, 4)),
        bundle_publish.PRESETS["hdemucs_mmi-ov0.25"],
        data,
    )

    assert (bundle / "waveform.json").read_text(encoding="utf-8") == '{"peaks": [1, 2]}\n'
    assert (bundle / "extras" / "note.txt").read_text(encoding="utf-8") == "hi\n"
    # Control: the old parts and manifest were replaced, not carried over.
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["preset"]["tag"] == "hdemucs_mmi-ov0.25", "the new manifest replaced the old one"
    assert (bundle / "vocals.flac").read_bytes() == (tmp_path / "fresh-4" / "vocals.flac").read_bytes()


def test_a_shared_swap_waits_for_the_read_barrier_so_no_reader_sees_the_gap(tmp_path: Path) -> None:
    """[if] a reader holds the eviction-claim lock [then] ``replace_bundle_dir`` leaves the bundle in place until it lets go, [else stop].

    The local worker, the GCE farm import, lyrics hydration and the farm
    publishes all swap through ``replace_bundle_dir``; a manifest or part
    request between its two renames would find no bundle (Codex on #6821).

    MUTATION TARGET: drop the ``readers_held_off`` hold in ``replace_bundle_dir``.
    """
    import threading
    import time

    from apps.cloud.stem_cache_budget import eviction_claim_lock_path
    from apps.cloud.stem_upload_queue import exclusive_file_lock
    from apps.stems.bundle_lock import replace_bundle_dir

    data_dir = tmp_path / "data"
    bundle = data_dir / "state" / "stems" / "sid"
    bundle.mkdir(parents=True)
    (bundle / "vocals.flac").write_bytes(b"old")
    new_dir = data_dir / "state" / ".stem-publish" / "sid-x"
    new_dir.mkdir(parents=True)
    (new_dir / "vocals.mp3").write_bytes(b"new")
    errors: list[BaseException] = []

    def swap() -> None:
        try:
            replace_bundle_dir(new_dir, bundle)
        except Exception as exc:  # noqa: BLE001 - reported to the main thread below
            errors.append(exc)

    worker = threading.Thread(target=swap)
    with exclusive_file_lock(eviction_claim_lock_path(data_dir, "sid")):
        worker.start()
        time.sleep(1.0)  # an unblocked swap takes milliseconds
        assert worker.is_alive(), "the swap moved the bundle while a reader held the barrier"
        assert (bundle / "vocals.flac").read_bytes() == b"old", "the bundle path was emptied under the barrier"
    worker.join(timeout=60)
    assert not errors, errors
    # Control: once released, the swap lands whole.
    assert sorted(p.name for p in bundle.iterdir()) == ["vocals.mp3"]


def test_control_an_out_dir_swap_takes_no_read_barrier(tmp_path: Path) -> None:
    """[if] the destination is a worker's ``--out-dir`` outside ``<data>/state`` [then] the swap needs no barrier and lands, [else stop]."""
    from apps.stems.bundle_lock import replace_bundle_dir

    dest = tmp_path / "out" / "bundle"
    dest.mkdir(parents=True)
    (dest / "vocals.flac").write_bytes(b"old")
    new_dir = tmp_path / "out" / "new"
    new_dir.mkdir()
    (new_dir / "vocals.flac").write_bytes(b"new")
    replace_bundle_dir(new_dir, dest)
    assert (dest / "vocals.flac").read_bytes() == b"new"
    assert not (tmp_path / "out" / "state").exists(), "no lock directory invented for an out-dir"


@pytest.mark.parametrize(
    ("platform", "mode"),
    [
        pytest.param("nt", "r+b", id="windows needs write access"),
        pytest.param("posix", "rb", id="posix stays read-only"),
    ],
)
def test_the_tree_flush_opens_files_in_a_mode_the_platform_can_fsync(platform: str, mode: str) -> None:
    """Windows' FlushFileBuffers refuses a read-only handle, so a publish there
    must open its staged files for write (Codex on #6820); POSIX keeps ``rb``.

    MUTATION TARGETS: always ``rb`` (Windows publish raises); always ``r+b``
    (a read-only staged file fails on POSIX, caught by the test below)."""
    from apps.stems.bundle_lock import file_flush_mode

    assert file_flush_mode(platform) == mode


def test_the_tree_flush_opens_every_file_through_the_platform_mode_and_leaves_it_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs the real flush: the bytes survive it (no truncating mode), and the
    open goes through :func:`file_flush_mode` (a stray hard-coded mode would
    bypass the Windows seam).

    MUTATION TARGETS: open with ``w+b`` (truncates); hard-code ``rb`` in the loop."""
    from apps.stems import bundle_lock

    part = tmp_path / "nested" / "vocals.mp3"
    part.parent.mkdir()
    part.write_bytes(b"ID3 payload")
    bundle_lock.fsync_tree(tmp_path)
    assert part.read_bytes() == b"ID3 payload"

    monkeypatch.setattr(bundle_lock, "file_flush_mode", lambda platform=None: "not-a-mode")
    with pytest.raises(ValueError, match="not-a-mode"):
        bundle_lock.fsync_tree(tmp_path)
