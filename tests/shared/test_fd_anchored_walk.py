"""Tests for the fd-anchored containment walk (:mod:`apps.shared.fd_anchored_walk`).

Split out of ``test_platform_paths.py`` for the same reason
``test_platform_paths_asset_resolver.py`` already was: this repo's
``file_size.max_python`` ratchet caps a module at 600 lines, and Amendment 17
says the fix for a budget breach is extraction into an already-imported
module, never a shrink of strings/tests/docs. These tests all exercise the
share-root symlink-escape / TOCTOU containment property -- packet 9h-T1
(closing the resolve()-then-check race PR #1271 review flagged) and its
sol-review v1 P1 follow-up (removing a second by-name lookup the first cut of
that fix introduced) -- so they belong together, separate from the rest of
``platform_paths``'s general path-resolution tests.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from apps.shared import fd_anchored_walk
from apps.shared import platform_paths as pp


@pytest.mark.skipif(pp.IS_WINDOWS, reason="symlink replacement fixture is POSIX-only")
def test_contained_asset_path_revalidates_share_containment_after_directory_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] a contained directory becomes an outside symlink [then ⛔️] it is rejected."""
    share_root = tmp_path / "share"
    target = share_root / "PIONEER" / "USB" / "ANLZ0000.DAT"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"dat")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ANLZ0000.DAT").write_bytes(b"outside")
    monkeypatch.setattr(pp, "SHARE_ROOT", share_root)
    mapped = pp.MappedPath(
        original="/PIONEER/USB/ANLZ0000.DAT",
        resolved=target,
        mapped=True,
        reason="share",
    )

    first = pp._contained_asset_path(mapped, target)
    replaced_directory = target.parent
    replaced_directory.rename(tmp_path / "USB-before-replacement")
    replaced_directory.symlink_to(outside, target_is_directory=True)
    second = pp._contained_asset_path(mapped, target)

    assert first.resolved == target
    assert second.resolved is None
    assert second.reason == "unsafe:share-symlink"


@pytest.mark.skipif(
    not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED,
    reason="fd-anchored walk needs O_DIRECTORY/O_NOFOLLOW + dir_fd support",
)
def test_fd_anchored_walk_catches_a_swap_that_lands_mid_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fix this packet ships for, proven the way the pin actually bites:
    a symlink swap that happens WHILE one ``resolve_asset_path`` call is
    still walking the path, not one already in place beforehand and not one
    that lands between two separate calls (that is
    ``test_contained_asset_path_revalidates_share_containment_after_directory_replacement``
    above -- a real regression guard, but a before-vs-after test, which is
    exactly what a per-request memo like ``AssetResolver`` cannot be blamed
    for failing to catch since nothing raced within its own call).

    Here the walk has ALREADY opened and validated ``PIONEER`` and ``USB``
    as real, non-symlink directories -- their fds are held -- and is about
    to open the leaf, ``ANLZ0000.DAT``, when the swap happens. The old
    ``candidate.resolve(strict=False)`` + ``is_relative_to`` pattern
    resolves the whole path in one call with nothing to hook mid-walk; the
    fd-anchored walk's own ``os.open`` IS the containment check, one
    filesystem call before the file is used, so there is a concrete place
    to prove the race is closed rather than merely narrowed.
    """
    share_root = tmp_path / "share"
    real_dir = share_root / "PIONEER" / "USB"
    real_dir.mkdir(parents=True)
    leaf = real_dir / "ANLZ0000.DAT"
    leaf.write_bytes(b"legitimate ANLZ bytes")
    outside = tmp_path / "outside.DAT"
    outside.write_bytes(b"attacker-controlled bytes")
    monkeypatch.setattr(pp, "SHARE_ROOT", share_root)

    real_os_open = pp.os.open
    swap_state = {"done": False}

    def _open_that_races_the_leaf(path, flags, *args, **kwargs):
        if path == "ANLZ0000.DAT" and not swap_state["done"]:
            # By the time this segment is being opened, the walk has
            # already opened+validated every ancestor (PIONEER, USB) as
            # real directories and is holding their fds -- this is the
            # instant a concurrent process would swap the leaf, i.e.
            # mid-resolution, not before the walk started.
            swap_state["done"] = True
            leaf.unlink()
            leaf.symlink_to(outside)
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(pp.os, "open", _open_that_races_the_leaf)

    result = pp.resolve_asset_path(
        "/PIONEER/USB/ANLZ0000.DAT", path_map=pp.PathMap(entries=())
    )

    assert swap_state["done"], "the test never exercised the race window"
    assert result.resolved is None
    assert result.reason == "unsafe:share-symlink"



@pytest.mark.skipif(
    not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED,
    reason="fd-anchored walk needs O_NOFOLLOW + dir_fd support",
)
def test_fd_anchored_walk_rejects_symlinked_directory_at_an_intermediate_segment(
    tmp_path: Path,
) -> None:
    """[if] a non-leaf, non-root segment is a symlinked directory [then bites]

    sol-review v1's P1 on this walk was specifically about an INTERMEDIATE
    segment (not the root, not the leaf) -- this pins that the walk still
    rejects one after dropping O_DIRECTORY from every open (the P1 fix).
    """
    root = tmp_path / "share"
    real_first = root / "PIONEER"
    real_first.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ANLZ0000.DAT").write_bytes(b"attacker payload")
    symlinked_middle = real_first / "USB"
    symlinked_middle.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        fd_anchored_walk.resolve_under_root(root / "PIONEER" / "USB" / "ANLZ0000.DAT", root)


@pytest.mark.skipif(
    not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED,
    reason="fd-anchored walk needs O_NOFOLLOW + dir_fd support",
)
def test_fd_anchored_walk_tolerates_a_real_file_where_a_directory_was_expected(
    tmp_path: Path,
) -> None:
    """[if] a non-leaf segment is a real, non-symlink FILE (not a directory)
    [then] the walk hands back the unresolved lexical remainder instead of
    raising -- this is legitimate data (resolve(strict=False) tolerated it
    too), and sol-review v1's own P1 warned against regressing it while
    fixing the adjacent second-lookup race. Proven meaningful below by
    showing the naive alternative fix (reject every non-directory) fails
    this exact assertion.
    """
    root = tmp_path / "share"
    real_first = root / "PIONEER"
    real_first.mkdir(parents=True)
    not_a_directory = real_first / "USB"
    not_a_directory.write_bytes(b"a real file, not a directory, not a symlink")

    resolved = fd_anchored_walk.resolve_under_root(
        root / "PIONEER" / "USB" / "ANLZ0000.DAT", root
    )

    assert resolved == not_a_directory.resolve() / "ANLZ0000.DAT"


def test_the_naive_reject_every_non_directory_fix_would_have_failed_the_above(
    tmp_path: Path,
) -> None:
    """Not a regression test on its own -- a meta-proof that the previous
    test is discriminating. sol-review v1's literal remedy text was "treat
    every ENOTDIR from the anchored walk as unsafe"; applied naively (reject
    every non-leaf non-directory, not just a genuine symlink) it would
    regress the real-file-where-a-directory-was-expected case. This
    reimplements that naive alternative standalone and shows it raises where
    the shipped fix does not, so the previous test is proven to actually
    discriminate between the two designs rather than passing either way.
    """
    root = tmp_path / "share"
    real_first = root / "PIONEER"
    real_first.mkdir(parents=True)
    not_a_directory = real_first / "USB"
    not_a_directory.write_bytes(b"a real file, not a directory, not a symlink")
    candidate = root / "PIONEER" / "USB" / "ANLZ0000.DAT"

    def _naive_reject_every_non_directory(candidate: Path, root: Path) -> Path:
        relative_parts = candidate.relative_to(root).parts
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        opened_fds = [root_fd]
        try:
            current_fd = root_fd
            for index, part in enumerate(relative_parts):
                is_last = index == len(relative_parts) - 1
                fd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current_fd)
                opened_fds.append(fd)
                if not is_last and not stat.S_ISDIR(os.fstat(fd).st_mode):
                    # The naive remedy: any non-directory here is unsafe.
                    raise OSError("naive fix: treats a real file as unsafe too")
                current_fd = fd
            return fd_anchored_walk.path_from_fd(current_fd)
        finally:
            for fd in reversed(opened_fds):
                os.close(fd)

    with pytest.raises(OSError):
        _naive_reject_every_non_directory(candidate, root)

    # The shipped fix, same fixture, does not raise -- this is the contrast
    # that proves the previous test is pinning a real design choice.
    fd_anchored_walk.resolve_under_root(candidate, root)


@pytest.mark.skipif(
    not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED,
    reason="fd-anchored walk needs O_NOFOLLOW + dir_fd support",
)
def test_no_second_by_name_lookup_exists_to_race_after_a_failed_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] a non-leaf segment is a symlinked directory [then ⛔️] the walk
    rejects it using only fds it already holds -- no second, name-based
    lookup exists anywhere in the walk for an attacker to race against.

    Rewritten self-contained (sol-review v1 BLOCKING P1, packet 9i-1): the
    previous version of this test loaded a frozen pre-fix copy of this
    module from ``/tmp/fd_anchored_walk_before_p1fix.py`` and silently
    SKIPPED whenever that file was absent -- which is every clean checkout
    and every normal CI runner, so it provided no actual regression
    coverage. Per the review's stated preference, this tests the CURRENT
    implementation's required behaviour directly instead of replaying
    history: patch `os.stat` (the by-name lookup the P1 fix removed) to
    explode if it is EVER called, then confirm `resolve_under_root` still
    correctly rejects a symlinked intermediate segment using only
    `os.fstat` on fds it already holds (see module docstring: dropping
    `O_DIRECTORY` means every segment opens with plain `O_NOFOLLOW`, and the
    directory-vs-file distinction is read off the fd already held, never a
    fresh name-based lookup). No external fixture, no environment
    dependency -- cannot silently skip.
    """
    root = tmp_path / "share"
    real_first = root / "PIONEER"
    real_first.mkdir(parents=True)
    middle = real_first / "USB"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ANLZ0000.DAT").write_bytes(b"attacker payload")
    middle.symlink_to(outside, target_is_directory=True)
    candidate = root / "PIONEER" / "USB" / "ANLZ0000.DAT"

    def _stat_must_not_be_called(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "fd_anchored_walk.resolve_under_root called os.stat by name -- "
            "the P1 fix removed this call precisely so nothing here can be "
            "raced; its reappearance is itself the regression"
        )

    monkeypatch.setattr(fd_anchored_walk.os, "stat", _stat_must_not_be_called)

    with pytest.raises(OSError):
        fd_anchored_walk.resolve_under_root(candidate, root)


def test_resolve_under_root_rejects_a_lexical_dotdot_escape_before_opening_anything(
    tmp_path: Path,
) -> None:
    """[if] a candidate carries a lexical '..' component that survives
    Path.relative_to() [then ⛔️] resolve_under_root rejects it with
    ValueError before opening anything -- sol-review v1 BLOCKING P1
    (packet 9i-1): ``Path.relative_to()`` does not normalize ``..``, so a
    candidate shaped like ``root/../outside/file`` still starts with
    root's own parts lexically and relative_to() happily returns parts
    beginning with '..'. Left unchecked the walk below would
    ``os.open('..', dir_fd=root_fd)``, stepping to root's own parent and
    out of containment before a single symlink check ever runs.
    """
    root = tmp_path / "share"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_bytes(b"attacker payload")
    candidate = Path(f"{root}/../outside/file")

    # Prove the premise first: relative_to() alone does NOT catch this.
    assert candidate.relative_to(root).parts[0] == ".."

    with pytest.raises(ValueError):
        fd_anchored_walk.resolve_under_root(candidate, root)


@pytest.mark.skipif(
    not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED,
    reason="fd-anchored walk needs O_NOFOLLOW + dir_fd support",
)
def test_resolve_under_root_rejects_a_root_swapped_for_a_symlink_after_first_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BLOCKING P1 (sol-review v1, packet 9j): the root itself is opened
    with plain ``O_DIRECTORY`` and no ``O_NOFOLLOW`` at all -- deliberately,
    since a configured root is allowed to be a symlink from the start (see
    ``test_resolve_under_root_still_works_when_root_is_a_symlink_from_the_start``
    below). But that same tolerance meant a root REPLACED with a symlink to
    an attacker directory, after having already been trusted, was followed
    exactly the same way, anchoring the whole walk in the wrong place with
    every descendant segment check passing. This proves the
    identity-checked anchor catches exactly that: the swap happens between
    one call that trusted the real root and a second call against the same
    configured root path, now a symlink elsewhere.
    """
    monkeypatch.setattr(fd_anchored_walk, "_ROOT_ANCHORS", {})

    root = tmp_path / "share"
    real_target = root / "PIONEER" / "USB" / "ANLZ0000.DAT"
    real_target.parent.mkdir(parents=True)
    real_target.write_bytes(b"legitimate")

    # First call: root is still the real directory -- establishes the anchor.
    first = fd_anchored_walk.resolve_under_root(
        root / "PIONEER" / "USB" / "ANLZ0000.DAT", root
    )
    assert first == real_target.resolve()

    # Swap the root itself (not a descendant -- the root) for a symlink
    # pointing at an attacker-controlled directory shaped identically
    # underneath, so every segment check below the root would otherwise
    # pass.
    outside = tmp_path / "outside"
    (outside / "PIONEER" / "USB").mkdir(parents=True)
    (outside / "PIONEER" / "USB" / "ANLZ0000.DAT").write_bytes(b"attacker-controlled")
    root.rename(tmp_path / "share-before-swap")
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        fd_anchored_walk.resolve_under_root(
            root / "PIONEER" / "USB" / "ANLZ0000.DAT", root
        )


@pytest.mark.skipif(
    not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED,
    reason="fd-anchored walk needs O_NOFOLLOW + dir_fd support",
)
def test_resolve_under_root_still_works_when_root_is_a_symlink_from_the_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blanket ``O_NOFOLLOW`` on the root open (the naive alternative fix
    to the P1 above) would reject THIS case outright: a root that is, and
    always was, a symlink to a real directory (SHARE_ROOT legitimately is
    one under remote mode -- see ``platform_paths.compute_share_root``).
    Proves the identity-checked anchor -- which only rejects a CHANGE in
    what root resolves to, never a symlink root per se -- keeps this
    working, called twice to prove the anchor established on the first
    call does not itself misfire on the second against an unchanged
    target.
    """
    monkeypatch.setattr(fd_anchored_walk, "_ROOT_ANCHORS", {})

    real_dir = tmp_path / "real-share"
    (real_dir / "PIONEER" / "USB").mkdir(parents=True)
    leaf = real_dir / "PIONEER" / "USB" / "ANLZ0000.DAT"
    leaf.write_bytes(b"legitimate")
    root = tmp_path / "share-symlink"
    root.symlink_to(real_dir, target_is_directory=True)

    first = fd_anchored_walk.resolve_under_root(
        root / "PIONEER" / "USB" / "ANLZ0000.DAT", root
    )
    second = fd_anchored_walk.resolve_under_root(
        root / "PIONEER" / "USB" / "ANLZ0000.DAT", root
    )

    assert first == leaf.resolve()
    assert second == leaf.resolve()


# ----- Root identity change: false-positive vs. hostile-swap (issue #1402) -


@pytest.mark.skipif(pp.IS_WINDOWS, reason="symlink/rename fixtures are POSIX-only")
def test_contained_asset_path_root_identity_change_gets_a_distinct_reason_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[if] the share root itself is recreated at its own unchanged path (a
    remount or a crate-sync rebuild, not an attacker swap) [then ⛔️]
    the result carries its own reason -- 'unsafe:root-identity-changed', never
    the hostile-swap 'unsafe:share-symlink' -- and the diagnostic reaches a
    log instead of being silently discarded (post-hoc review of PR #1357)."""
    monkeypatch.setattr(fd_anchored_walk, "_ROOT_ANCHORS", {})
    share_root = tmp_path / "share"
    target = share_root / "PIONEER" / "USB" / "ANLZ0000.DAT"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"dat")
    monkeypatch.setattr(pp, "SHARE_ROOT", share_root)
    mapped = pp.MappedPath(
        original="/PIONEER/USB/ANLZ0000.DAT", resolved=target, mapped=True, reason="share"
    )

    first = pp._contained_asset_path(mapped, target)

    # Recreate the root at the SAME configured path -- a fresh directory
    # (different st_ino, same st_dev) with identical, legitimate content
    # underneath. This is what a crate re-sync or `rsync --delete` rebuild
    # of pioneer-share does; it is not an attacker directory.
    rebuilt = tmp_path / "share-rebuilt"
    (rebuilt / "PIONEER" / "USB").mkdir(parents=True)
    (rebuilt / "PIONEER" / "USB" / "ANLZ0000.DAT").write_bytes(b"dat")
    share_root.rename(tmp_path / "share-before-rebuild")
    rebuilt.rename(share_root)

    with caplog.at_level("ERROR", logger="apps.shared.fd_anchored_walk"):
        second = pp._contained_asset_path(mapped, target)

    assert first.resolved == target
    assert second.resolved is None
    assert second.mapped is False
    assert second.reason == "unsafe:root-identity-changed"
    assert "no longer resolves to the identity" in caplog.text


@pytest.mark.skipif(
    not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED,
    reason="fd-anchored walk needs O_NOFOLLOW + dir_fd support",
)
def test_reset_root_anchor_lets_the_next_call_re_anchor_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] ``reset_root_anchor`` is called for a root between two calls
    [then ⛔️] the changed identity is trusted fresh instead of
    raising ``RootIdentityChanged`` -- the explicit invalidation seam a
    deliberate library-mode/crate change needs (issue #1402 fix shape)."""
    monkeypatch.setattr(fd_anchored_walk, "_ROOT_ANCHORS", {})
    root = tmp_path / "share"
    leaf = root / "PIONEER" / "USB" / "ANLZ0000.DAT"
    leaf.parent.mkdir(parents=True)
    leaf.write_bytes(b"legitimate")
    fd_anchored_walk.resolve_under_root(leaf, root)

    rebuilt = tmp_path / "share-rebuilt"
    (rebuilt / "PIONEER" / "USB").mkdir(parents=True)
    (rebuilt / "PIONEER" / "USB" / "ANLZ0000.DAT").write_bytes(b"legitimate")
    root.rename(tmp_path / "share-before-rebuild")
    rebuilt.rename(root)

    with pytest.raises(fd_anchored_walk.RootIdentityChanged):
        fd_anchored_walk.resolve_under_root(leaf, root)

    fd_anchored_walk.reset_root_anchor(root)

    resolved = fd_anchored_walk.resolve_under_root(leaf, root)
    assert resolved == leaf.resolve()


@pytest.mark.skipif(pp.IS_WINDOWS, reason="symlink/rename fixtures are POSIX-only")
def test_refresh_share_root_resets_the_identity_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] ``platform_paths.refresh_share_root`` runs between two asset
    resolutions (a deliberate library-mode/crate change) [then ⛔️]
    a root recreated in that window re-anchors instead of tripping
    ``RootIdentityChanged`` -- the wiring the fix shape requires."""
    monkeypatch.setattr(fd_anchored_walk, "_ROOT_ANCHORS", {})
    share_root = tmp_path / "share"
    target = share_root / "PIONEER" / "USB" / "ANLZ0000.DAT"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"dat")
    monkeypatch.setattr(pp, "SHARE_ROOT", share_root)
    mapped = pp.MappedPath(
        original="/PIONEER/USB/ANLZ0000.DAT", resolved=target, mapped=True, reason="share"
    )
    pp._contained_asset_path(mapped, target)

    rebuilt = tmp_path / "share-rebuilt"
    (rebuilt / "PIONEER" / "USB").mkdir(parents=True)
    (rebuilt / "PIONEER" / "USB" / "ANLZ0000.DAT").write_bytes(b"dat")
    share_root.rename(tmp_path / "share-before-rebuild")
    rebuilt.rename(share_root)

    monkeypatch.setattr(pp, "compute_share_root", lambda: share_root)
    pp.refresh_share_root()

    result = pp._contained_asset_path(mapped, target)
    assert result.resolved == target
    assert result.reason == "share"
