"""Stem bundle swaps under recompression and republish (STEM-58).

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
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.stems import recompress as rc
from tests.stems.recompress_support import (
    PARTS,
    RATE,
    _data_dir,
    _flac_bundle,
    _fresh_parts,
    _noise,
    _publish_result,
    _snapshot,
    _source,
    _the_two_flac_publishers,
    sf,
)

pytestmark = [pytest.mark.requirement("STEM-58"), pytest.mark.requires_audio_stack]


def test_a_first_publish_creates_no_swap_directory(tmp_path: Path) -> None:
    """[if] nothing is at the destination yet [then] the swap creates no backup directory, [else stop].

    MUTATION TARGET: create the backup's parent unconditionally, and a first
    publish to a shallow ``--out-dir`` such as ``/tmp/bundle`` tries to create
    ``/.stem-bundle-swap`` and fails without root (Codex on #6820).
    """
    from apps.stems.bundle_lock import replace_bundle_dir, swap_backup_path

    new = tmp_path / "staged"
    new.mkdir()
    (new / "manifest.json").write_text("{}")
    dest = tmp_path / "data" / "state" / "stems" / "bundle"
    dest.parent.mkdir(parents=True)

    replace_bundle_dir(new, dest)

    assert (dest / "manifest.json").read_text() == "{}"
    assert not swap_backup_path(dest).parent.parent.exists(), "no .stem-bundle-swap created"


def test_a_bundle_outside_the_store_keeps_its_swap_backup_beside_it(tmp_path: Path) -> None:
    """[if] a bundle outside <data>/state is replaced [then] its backup lives in the bundle's own parent, [else stop].

    MUTATION TARGET: use the store layout for every destination, and a second
    publish to a shallow --out-dir such as /tmp/bundle tries to create
    /.stem-bundle-swap and fails without root (Codex on #6820).
    """
    from apps.stems.bundle_lock import replace_bundle_dir, swap_backup_path

    out = tmp_path / "out"
    out.mkdir()
    dest = out / "bundle"
    dest.mkdir()
    (dest / "manifest.json").write_text("old")
    new = tmp_path / "staged"
    new.mkdir()
    (new / "manifest.json").write_text("new")

    assert swap_backup_path(dest).parent == out
    replace_bundle_dir(new, dest)

    assert (dest / "manifest.json").read_text() == "new"
    assert sorted(p.name for p in out.iterdir()) == ["bundle"], "no backup left, and none outside out/"


def test_control_a_store_bundle_keeps_its_swap_backup_outside_the_stem_root(tmp_path: Path) -> None:
    """[if] the bundle is in <data>/state/<stem root> [then] its backup is outside every stem root, [else stop]."""
    from apps.stems.bundle_lock import SWAP_DIR_NAME, swap_backup_path

    dest = tmp_path / "data" / "state" / "stems" / "sid"
    # The real location: tmp_path itself may sit under a symlink (macOS /var).
    state = (tmp_path / "data" / "state").resolve()
    assert swap_backup_path(dest) == state / SWAP_DIR_NAME / "stems" / "sid"


def test_two_data_dirs_on_sibling_stem_roots_never_share_a_swap_backup(tmp_path: Path) -> None:
    """[if] two data dirs' stem roots symlink to sibling directories on one disk [then] each keeps its own swap backup, [else stop].

    Both roots are named ``stems`` in their data dirs and resolve to
    ``disk/A`` and ``disk/B``, so the directory beside them is shared and each
    writer holds a different data-dir lock. A's interrupted swap leaves its
    only good copy aside; B publishing the same stable id must neither restore
    it into B nor discard it, and A's next read still puts it back (Codex on
    #6820). MUTATION TARGET: name the namespace after the lexical
    ``dest.parent.name`` in ``swap_backup_path``.
    """
    import os

    from apps.stems.bundle_lock import replace_bundle_dir, restore_orphaned_swap, swap_backup_path

    disk = tmp_path / "disk"
    datas = {}
    for name in ("A", "B"):
        (disk / name).mkdir(parents=True)
        data = tmp_path / f"data-{name}"
        (data / "state").mkdir(parents=True)
        (data / "state" / "stems").symlink_to(disk / name, target_is_directory=True)
        datas[name] = data
    a_bundle = _flac_bundle(datas["A"], "x", _source(tmp_path, "a"))
    a_before = _snapshot(a_bundle)
    a_backup = swap_backup_path(a_bundle)
    b_dest = datas["B"] / "state" / "stems" / "x"
    assert a_backup != swap_backup_path(b_dest), "each real stem root has its own namespace"
    assert a_backup.parent.parent == swap_backup_path(b_dest).parent.parent, "precondition: one shared swap dir"
    a_backup.parent.mkdir(parents=True)
    os.rename(a_bundle, a_backup)  # A's writer died between its two renames

    staged = tmp_path / "b-render"
    staged.mkdir()
    for i, part in enumerate(PARTS):
        sf.write(staged / f"{part}.flac", _noise(i + 7), RATE, format="FLAC", subtype="PCM_16")
    b_new = _snapshot(staged)
    replace_bundle_dir(staged, b_dest)

    assert _snapshot(b_dest) == b_new, "B holds its own render, not A's old bundle"
    assert _snapshot(a_backup) == a_before, "A's only good copy is still aside"
    assert restore_orphaned_swap(a_bundle, datas["A"]), "A's next read puts it back"
    assert _snapshot(a_bundle) == a_before


def test_the_swap_journal_reaches_the_disk_before_the_bundle_moves() -> None:
    """[if] a pass journals a swap [then] the journal file is fsynced, then replaced into place, then its directory fsynced, [else stop].

    After a power loss between moving the live bundle aside and landing the
    new one, the journal is the only record of where the bundle went (Codex on
    #6820). A crash cannot be staged in a test, so the order is read from the
    real function's source, and the journal it writes is checked for real.

    MUTATION TARGETS: drop the file fsync, or the directory fsync, or move the
    file fsync after the replace.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(rc._write_swap_journal)))
    order = [
        node.lineno
        for name in ("fsync", "replace", "_fsync_dir")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == name)
            or (isinstance(node.func, ast.Name) and node.func.id == name)
        )
    ]
    assert len(order) == 3, f"expected one fsync, one replace and one directory fsync, found lines {order}"
    assert order == sorted(order), "the journal must be flushed, then replaced, then its directory flushed"
    dir_tree = ast.parse(textwrap.dedent(inspect.getsource(rc._fsync_dir)))
    assert any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "fsync"
        for n in ast.walk(dir_tree)
    ), "_fsync_dir does not fsync"


def test_the_journal_directories_a_first_swap_creates_are_flushed_into_their_parents() -> None:
    """[if] a first swap creates the journal's directories [then] each is flushed into its parent before the write, [else stop].

    Flushing only the leaf directory leaves the leaf's own entry, and its
    parents', unflushed (Codex on #6820). As above, a crash cannot be staged,
    so the order is read from the real source.

    MUTATION TARGETS: go back to ``mkdir(parents=True)``; drop the per-entry
    flush; flush the new directory instead of its parent.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(rc._write_swap_journal)))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(isinstance(c.func, ast.Name) and c.func.id == "_mkdir_durably" for c in calls), (
        "the journal directory is not made durably"
    )
    assert not [c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr == "mkdir"], (
        "a plain mkdir creates journal directories"
    )
    helper = ast.parse(textwrap.dedent(inspect.getsource(rc._mkdir_durably)))
    loops = [n for n in ast.walk(helper) if isinstance(n, ast.For)]
    assert len(loops) == 1, "expected one loop over the created directories"
    body = [n for stmt in loops[0].body for n in ast.walk(stmt) if isinstance(n, ast.Call)]
    mkdirs = [c for c in body if isinstance(c.func, ast.Attribute) and c.func.attr == "mkdir"]
    flushes = [c for c in body if isinstance(c.func, ast.Name) and c.func.id == "_fsync_dir"]
    assert mkdirs and flushes, "each created directory must be flushed as it is made"
    assert all(ast.unparse(f.args[0]).endswith(".parent") for f in flushes), (
        "the flush must be of the parent that holds the new entry"
    )


def test_every_swap_rename_and_journal_removal_is_flushed_before_the_next_step() -> None:
    """[if] a swap or recovery moves a bundle or drops its journal [then] each step is flushed to its directories, [else stop].

    Flushing only the journal lets it survive a power loss that the moves it
    describes do not, and recovery then looks for a backup that is not there
    (Codex P1 on #6821). As above, the order is read from the real source.

    MUTATION TARGETS: a bare ``.rename``/``.unlink`` in ``_swap_in`` or
    ``recover_orphaned_swaps``; drop either parent flush in ``_rename_durably``;
    drop the flush in ``_remove_journal``; a plain mkdir for the backup parent.
    """
    import ast
    import inspect
    import textwrap

    def calls(fn: object) -> list[ast.Call]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))  # type: ignore[arg-type]
        return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    def named(found: list[ast.Call], name: str) -> list[ast.Call]:
        return [
            c
            for c in found
            if (isinstance(c.func, ast.Name) and c.func.id == name)
            or (isinstance(c.func, ast.Attribute) and c.func.attr == name)
        ]

    for fn in (rc._swap_in, rc.recover_orphaned_swaps):
        found = calls(fn)
        assert named(found, "_rename_durably") and named(found, "_remove_journal"), (
            f"{fn.__name__}: the probe found no durable step"
        )
        assert not named(found, "rename"), f"{fn.__name__} renames without flushing"
        assert not named(found, "unlink"), f"{fn.__name__} drops a journal without flushing"
        assert not named(found, "mkdir"), f"{fn.__name__} makes a directory without flushing it"
    flushed = [ast.unparse(c.args[0]) for c in named(calls(rc._rename_durably), "fsync_dir")]
    assert sorted(flushed) == ["destination.parent", "source.parent"], f"_rename_durably flushes {flushed}"
    assert [ast.unparse(c.args[0]) for c in named(calls(rc._remove_journal), "_fsync_dir")] == ["journal.parent"]


def test_the_new_bundle_is_flushed_before_its_swap_is_journaled() -> None:
    """[if] a swap is about to move bundles [then] the new bundle's files and directories are flushed first, [else stop].

    Renames flush only directory entries: a power loss after the journal is
    removed could leave the live parts empty with no record of the intact
    backup (Codex on #6820). Order read from the real source, as above.

    MUTATION TARGETS: drop ``_fsync_tree``; call it after the journal; skip
    files or directories inside it.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(rc._swap_in)))
    lines = {
        name: [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
        ]
        for name in ("_fsync_tree", "_write_swap_journal", "_rename_durably")
    }
    assert lines["_fsync_tree"], "_swap_in does not flush the new bundle"
    assert max(lines["_fsync_tree"]) < min(lines["_write_swap_journal"]) < min(lines["_rename_durably"])
    helper = ast.parse(textwrap.dedent(inspect.getsource(rc._fsync_tree)))
    names = {
        (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
        for n in ast.walk(helper)
        if isinstance(n, ast.Call)
    }
    assert {"fsync", "fsync_dir", "rglob"} <= names, f"_fsync_tree calls only {sorted(names)}"


def test_a_published_bundle_is_flushed_before_its_backup_is_deleted() -> None:
    """[if] replace_bundle_dir swaps a bundle in [then] the new tree and both renames reach the disk before the old bundle is deleted, [else stop].

    A power loss after that delete must not keep the renames and lose the
    new parts, leaving no usable bundle and no backup (Codex on #6820).
    Order read from the real source, as above: a power loss cannot be
    staged in a test.

    MUTATION TARGETS: drop ``fsync_tree``; call it after the swap's rename;
    drop the flush between the two renames; discard the backup before
    flushing the parents.
    """
    import ast
    import inspect
    import textwrap

    from apps.stems import bundle_lock

    tree = ast.parse(textwrap.dedent(inspect.getsource(bundle_lock.replace_bundle_dir)))
    calls: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            calls.setdefault(name, []).append(node.lineno)
    (swap_rename,) = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "rename"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "new_dir"
    ]
    assert calls.get("fsync_tree"), "replace_bundle_dir does not flush the new bundle"
    assert max(calls["fsync_tree"]) < swap_rename
    (aside_rename,) = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "rename"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "dest"
    ]
    flushes = {
        (node.lineno, ast.unparse(node.args[0]))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "fsync_dir"
    }
    # The move aside is flushed in both parents before the second rename
    # (Codex on #6820), and the swap's parent after it, before the backup is
    # deleted.
    between = {arg for line, arg in flushes if aside_rename < line < swap_rename}
    assert {"backup.parent", "dest.parent"} <= between, f"the move aside is flushed before the swap: {between}"
    after = [line for line, arg in flushes if line > swap_rename and arg == "dest.parent"]
    assert after, "the swap is flushed"
    assert max(after) < max(calls["_discard_backup"])


def test_a_rollback_or_repair_of_a_swap_is_flushed_before_it_reports() -> None:
    """[if] replace_bundle_dir rolls a failed swap back, or repairs an orphaned one [then] the rename back is flushed in both parents, [else stop].

    A power loss after an unflushed rename back could leave the bundle path
    empty although the call reported the old bundle kept (Sol on #6820).
    Order read from the real source, as above: a power loss cannot be staged
    in a test. ``undo_failed_swap`` renames everything before it flushes, so
    its flushes are checked on disk by
    ``test_a_rollback_whose_flushes_keep_failing_still_puts_the_old_bundle_back``.

    MUTATION TARGETS: a bare ``backup.rename(dest)`` in either function;
    drop either flush from ``_rename_durably``.
    """
    import ast
    import inspect
    import textwrap

    from apps.stems import bundle_lock

    def calls(function: Callable[..., object]) -> list[ast.Call]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    for function, expected in (
        (bundle_lock.replace_bundle_dir, 1),
        (bundle_lock.restore_orphaned_swap, 1),
    ):
        found = calls(function)
        bare = [
            ast.unparse(c)
            for c in found
            if isinstance(c.func, ast.Attribute) and c.func.attr == "rename" and ast.unparse(c.func.value) == "backup"
        ]
        assert not bare, f"{function.__name__} renames the backup back without flushing it: {bare}"
        durable = [ast.unparse(c) for c in found if getattr(c.func, "id", "") == "_rename_durably"]
        assert durable.count("_rename_durably(backup, dest)") == expected, durable

    body = calls(bundle_lock._rename_durably)
    order = [ast.unparse(c) for c in sorted(body, key=lambda c: (c.lineno, c.col_offset))]
    assert order[0] == "source.rename(destination)"
    assert {"fsync_dir(destination.parent)", "fsync_dir(source.parent)"} <= set(order[1:]), order


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
