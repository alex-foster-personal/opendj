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

from collections.abc import Callable
from pathlib import Path

import pytest

from apps.stems import recompress as rc
from tests.stems.recompress_support import (
    PARTS,
    RATE,
    _flac_bundle,
    _noise,
    _snapshot,
    _source,
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


def test_recompress_uses_the_sole_durable_rename_from_bundle_lock() -> None:
    """[if] the two modules are inspected [then] recompress holds bundle_lock's durable rename, not a copy, [else stop].

    MUTATION TARGET: a local ``def _rename_durably`` in ``recompress.py``.
    """
    from apps.stems import bundle_lock

    assert rc._rename_durably is bundle_lock._rename_durably
    assert "def _rename_durably" not in Path(rc.__file__).read_text(encoding="utf-8")


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
