"""One bad row never fails the page, and nothing weak is remembered (LIBM-137, round 4).

[if] a malformed, hard-linked or unnameable row asset fails the page or is remembered unsafely [then] fail, [else stop].

Second independent review of the row asset cache. Same rules as
``test_row_assets_cache.py``, whose fixture this file shares: real files, real
links, and a preview level that says where the bytes came from (5 inside the
share root, 9 a second library, 13 outside).

Regression one-liners:
  - if a zero-byte, garbage, truncated or bad-header analysis file raises out of the row read then broken
  - if a malformed file is logged with its path instead of the track's stable id then broken
  - if a vendor string that cannot name a file raises out of the row read then broken
  - if a row on the uncached path reads or fills the path-keyed preview or vocals memory then broken
  - if a hard-linked analysis file is read or remembered then broken
  - if an analysis file larger than 16 MiB is read then broken
  - if a listing trusts a share root that became another directory, by rename or through a symlinked parent, then broken
  - if the explicit re-anchor call trusts a root below a symlinked ancestor then broken
  - if the explicit re-anchor call leaves the resolved-form anchor or a descriptor behind then broken
  - if a share root that is, or became, a symlink is trusted without being asked to then broken
  - if a row whose chain stopped early is read again on the next page then broken
  - if a missing share root reports a cover as unresolved then broken
"""
from __future__ import annotations

import logging
import os
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.shared import fd_anchored_walk, platform_paths
from apps.shared.platform_paths import AssetResolver
from apps.webui.server.rb_vendor_pkg import anlz, row_assets
from apps.webui.server.rb_vendor_pkg import row_hydration_cache as rhc
from tests.webui.test_row_assets_cache import (
    ADP,
    ANLZ_DIR,
    IMG,
    INSIDE,
    OTHER_LIBRARY,
    OUTSIDE,
    _meta,
    _pmai,
    _section,
    _strip,
    build,
    call,
    dat,
    forget_everything,
    level_of,
    two_ex,
    use_root,
)

pytestmark = [
    pytest.mark.requirement("LIBM-137"),
    pytest.mark.skipif(
        not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED, reason="needs the fd-anchored walk"
    ),
]

SID = "sid-under-test"


@pytest.fixture
def share(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """The one-row share root of ``test_row_assets_cache.py``, anchored afresh."""
    root = (tmp_path / "share").resolve()
    build(root, INSIDE)
    use_root(monkeypatch, root)
    forget_everything()
    yield root
    forget_everything()
    fd_anchored_walk.reset_root_anchors()


#: A row the plain-path plan refuses (an empty component), so it takes the
#: uncached path. The resolver collapses the doubled slash onto the same files.
ADP_UNCACHED = ADP.replace("/USBANLZ/", "/USBANLZ//")
IMG_UNCACHED = IMG.replace("/Artwork/", "/Artwork//")


def bad_pvdi_header() -> bytes:
    """A good tri-band preview beside a PVDI section whose fixed header moved."""
    preview = _section(b"PWV6", struct.pack(">II", 3, 1200), bytes([INSIDE] * 3) * 1200)
    head = bytes.fromhex("00000400562200ff") + struct.pack(">I", 4)
    return _pmai(preview, _section(b"PVDI", head, bytes(4)))


MALFORMED: dict[str, bytes] = {
    "zero-byte": b"",
    "garbage": b"\xde\xad\xbe\xef" * 64,
    "truncated-header": b"PMAI\x00\x00",
    "truncated-section": two_ex(INSIDE)[:40],
    "short-preview": _pmai(_section(b"PWV6", struct.pack(">II", 3, 10), bytes(30))),
}


# ----- P1-a: a malformed analysis file degrades its own row -------------------


@pytest.mark.parametrize("kind", sorted(MALFORMED))
@pytest.mark.parametrize("adp", [ADP, ADP_UNCACHED], ids=["walked", "uncached"])
def test_malformed_2ex_falls_through_to_the_next_source(share: Path, kind: str, adp: str) -> None:
    (share / ANLZ_DIR / "ANLZ0000.2EX").write_bytes(MALFORMED[kind])
    for _attempt in ("cold", "warm"):
        got = call(_meta(adp=adp))  # must not raise
        assert level_of(got) == INSIDE, "the .DAT beside it still gives the preview"
        assert got.pvdi_vocals == {"status": "not_analyzed"}
        assert (got.artwork_available, got.artwork_status) == (True, "ok")


@pytest.mark.parametrize("adp", [ADP, ADP_UNCACHED], ids=["walked", "uncached"])
def test_bad_pvdi_header_keeps_the_preview_and_drops_the_vocals(share: Path, adp: str) -> None:
    (share / ANLZ_DIR / "ANLZ0000.2EX").write_bytes(bad_pvdi_header())
    got = call(_meta(adp=adp))
    assert level_of(got) == INSIDE
    assert got.pvdi_vocals == {"status": "not_analyzed"}


@pytest.mark.parametrize("adp", [ADP, ADP_UNCACHED], ids=["walked", "uncached"])
def test_every_source_malformed_is_a_row_without_a_preview(share: Path, adp: str) -> None:
    for name in ("ANLZ0000.2EX", "ANLZ0000.DAT"):
        (share / ANLZ_DIR / name).write_bytes(MALFORMED["garbage"])
    got = call(_meta(adp=adp))
    assert (got.preview_b64, got.preview_max) == (None, None)
    assert got.pvdi_vocals == {"status": "not_analyzed"}
    assert got.artwork_status == "ok"


@pytest.mark.parametrize("adp", [ADP, ADP_UNCACHED], ids=["walked", "uncached"])
def test_an_index_error_out_of_a_reader_degrades_the_row(
    share: Path, adp: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected: no file was found that makes the shipped readers index past
    their buffer, so the first reader is replaced by one that does."""

    def past_the_end(source: object) -> None:
        raise IndexError("index out of range")

    sources = (("." + "2EX", past_the_end), *anlz._PREVIEW_SOURCES[1:])
    monkeypatch.setattr(anlz, "_PREVIEW_SOURCES", sources)
    got = call(_meta(adp=adp))
    assert level_of(got) == INSIDE, "the .DAT still answers"


def test_a_malformed_row_is_remembered_and_heals_when_the_file_does(share: Path) -> None:
    twoex = share / ANLZ_DIR / "ANLZ0000.2EX"
    twoex.write_bytes(MALFORMED["garbage"])
    assert call().pvdi_vocals == {"status": "not_analyzed"}
    assert len(row_assets._ROW_ASSETS) == 1, "its files were read cleanly: the answer is stable"
    twoex.write_bytes(two_ex(INSIDE))
    assert call().pvdi_vocals["status"] == "rekordbox"


@pytest.mark.parametrize("adp", [ADP, ADP_UNCACHED], ids=["walked", "uncached"])
def test_a_malformed_file_is_logged_by_stable_id_never_by_path(
    share: Path, adp: str, caplog: pytest.LogCaptureFixture
) -> None:
    (share / ANLZ_DIR / "ANLZ0000.2EX").write_bytes(MALFORMED["garbage"])
    with caplog.at_level(logging.WARNING):
        call(_meta(adp=adp))
    said = [r.getMessage() for r in caplog.records if "unreadable" in r.getMessage()]
    assert said, "a malformed file is logged"
    assert all(SID in line for line in said)
    assert not any("0000A001" in line or str(share) in line for line in said)


@pytest.mark.parametrize(
    "vendor",
    [
        "/PIONEER/USBANLZ/P001/\ud800/ANLZ0000.DAT",
        "/PIONEER/USBANLZ/P001/a\x00b/ANLZ0000.DAT",
        "/elsewhere/\ud800/ANLZ0000.DAT",
    ],
    ids=["lone-surrogate", "nul", "unmapped-surrogate"],
)
def test_a_vendor_string_that_cannot_name_a_file_degrades_the_row(share: Path, vendor: str) -> None:
    bad = call(_meta(adp=vendor, img=vendor))  # must not raise
    assert (bad.preview_b64, bad.artwork_available) == (None, False)
    assert bad.pvdi_vocals == {"status": "not_analyzed"}
    assert level_of(call()) == INSIDE, "the neighbor is untouched"


def test_one_bad_row_leaves_its_neighbors_in_the_same_pass_intact(share: Path) -> None:
    (share / ANLZ_DIR / "ANLZ0000.2EX").write_bytes(MALFORMED["zero-byte"])
    (share / ANLZ_DIR / "ANLZ0000.DAT").write_bytes(MALFORMED["garbage"])
    second = share / "PIONEER/USBANLZ/P001/0000B002"
    second.mkdir()
    (second / "ANLZ0000.2EX").write_bytes(two_ex(INSIDE))
    (second / "ANLZ0000.DAT").write_bytes(dat(INSIDE))
    metas = {
        "bad": _meta(),
        "good": _meta(adp="/PIONEER/USBANLZ/P001/0000B002/ANLZ0000.DAT"),
        "odd": _meta(adp="/PIONEER/USBANLZ/P001/\ud800/ANLZ0000.DAT"),
    }
    rows = row_assets.bulk_rb_row_assets(metas, resolver=AssetResolver())
    assert rows["bad"].preview_b64 is None
    assert level_of(rows["good"]) == INSIDE
    assert rows["good"].pvdi_vocals["status"] == "rekordbox"
    assert rows["odd"].preview_b64 is None


# ----- P2-a: the uncached path remembers nothing ------------------------------


def test_the_uncached_path_fills_no_path_keyed_memory(share: Path) -> None:
    got = call(_meta(adp=ADP_UNCACHED, img=IMG_UNCACHED))
    assert level_of(got) == INSIDE, "control: the uncached row does read its files"
    assert got.pvdi_vocals["status"] == "rekordbox"
    assert (len(rhc._PREVIEW_CACHE), len(rhc._VOCALS_CACHE)) == (0, 0)
    assert len(row_assets._ROW_ASSETS) == 0


def test_the_uncached_path_serves_nothing_from_path_keyed_memory(share: Path) -> None:
    """Whatever is already in the path-keyed memory, however it got there, is
    not what an uncached listing row answers with."""
    twoex = share / ANLZ_DIR / "ANLZ0000.2EX"
    mtime = twoex.stat().st_mtime
    rhc._PREVIEW_CACHE.put(ADP_UNCACHED, str(twoex), mtime, (_strip(OUTSIDE), OUTSIDE))
    rhc._VOCALS_CACHE.put(str(twoex), str(twoex), mtime, {"status": "no_vocals", "planted": True})
    got = call(_meta(adp=ADP_UNCACHED))
    assert level_of(got) == INSIDE
    assert got.pvdi_vocals["status"] == "rekordbox"


# ----- P3: hard links and the size bound --------------------------------------


def test_a_hard_link_to_an_outside_file_is_not_read_or_remembered(
    share: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-file.2EX"
    outside.write_bytes(two_ex(OUTSIDE))
    inside = share / ANLZ_DIR / "ANLZ0000.2EX"
    inside.unlink()
    os.link(outside, inside)
    assert inside.stat().st_nlink == 2
    for _attempt in ("cold", "warm"):
        got = call()
        assert level_of(got) == INSIDE, "the .DAT is read; the linked .2EX is not"
        assert got.pvdi_vocals == {"status": "not_analyzed"}
        assert len(row_assets._ROW_ASSETS) == 0, "a refused file means nothing is remembered"
    outside.unlink()  # one name left: an ordinary file again
    healed = call()
    assert level_of(healed) == OUTSIDE and len(row_assets._ROW_ASSETS) == 1


def test_read_leaf_refuses_a_hard_linked_file(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x")
    os.link(tmp_path / "a", tmp_path / "b")
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        got = fd_anchored_walk.read_leaf(fd, "a", 1024)
        assert got is not None and got[1] is None and got[0].st_nlink == 2
        os.unlink(tmp_path / "b")
        assert fd_anchored_walk.read_leaf(fd, "a", 1024)[1] == b"x"  # type: ignore[index]
    finally:
        os.close(fd)


def test_the_size_bound_fits_real_files_with_headroom(share: Path) -> None:
    """The largest analysis file found in three real export trees (81,075
    files, Thu 1 Oct 2026) was 3,761,017 bytes."""
    assert row_assets.MAX_ASSET_BYTES == 16 * 1024 * 1024
    twoex = share / ANLZ_DIR / "ANLZ0000.2EX"
    with twoex.open("ab") as handle:
        handle.truncate(row_assets.MAX_ASSET_BYTES + 1)
    got = call()
    assert level_of(got) == INSIDE and got.pvdi_vocals == {"status": "not_analyzed"}
    assert len(row_assets._ROW_ASSETS) == 0
    with twoex.open("ab") as handle:
        handle.truncate(row_assets.MAX_ASSET_BYTES)
    call()
    assert len(row_assets._ROW_ASSETS) == 1, "a file at the bound is read"


# ----- a share root that became another directory (LIBM-139, round 5) ---------


def _nested_share(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """``crate/share``: the shape of a remote-mode share root, one level down."""
    root = (tmp_path / "crate" / "share").resolve()
    build(root, INSIDE)
    use_root(monkeypatch, root)
    forget_everything()
    return root


def _swap_parent_for_a_symlink(root: Path, tmp_path: Path) -> None:
    build(tmp_path / "evil" / "share", OUTSIDE)
    os.rename(root.parent, tmp_path / "crate-moved-aside")
    os.symlink(tmp_path / "evil", root.parent)


@pytest.mark.requirement("LIBM-139")
def test_a_symlinked_parent_does_not_get_another_tree_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _nested_share(monkeypatch, tmp_path)
    try:
        assert level_of(call()) == INSIDE
        anchored = fd_anchored_walk._ROOT_ANCHORS[str(root)]
        _swap_parent_for_a_symlink(root, tmp_path)
        assert (root / ANLZ_DIR / "ANLZ0000.DAT").is_file(), "control: the path now names the other tree"
        for _attempt in range(2):
            refused = call()
            assert level_of(refused) is None
            assert refused.artwork_status == "unresolved"
        assert fd_anchored_walk._ROOT_ANCHORS[str(root)] == anchored
        assert platform_paths.resolve_asset_path(ADP).reason == "unsafe:root-identity-changed"
    finally:
        forget_everything()
        fd_anchored_walk.reset_root_anchors()


@pytest.mark.requirement("LIBM-139")
def test_a_root_swapped_by_rename_is_refused_until_the_explicit_call(
    share: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert level_of(call()) == INSIDE
    anchored = fd_anchored_walk._ROOT_ANCHORS[str(share)]
    os.rename(share, tmp_path / "unplugged")
    build(share, OTHER_LIBRARY)  # the same path, a new real directory
    with caplog.at_level(logging.ERROR, logger="apps.shared.fd_anchored_walk"):
        for _attempt in range(2):
            refused = call()
            assert level_of(refused) is None
            assert refused.artwork_status == "unresolved"
    assert any("refusing to walk" in r.getMessage() for r in caplog.records)
    assert fd_anchored_walk._ROOT_ANCHORS[str(share)] == anchored, "a listing never moves the anchor"
    assert platform_paths.resolve_asset_path(ADP).reason == "unsafe:root-identity-changed"

    assert fd_anchored_walk.reanchor_root(share) is True
    here = os.stat(share)
    assert fd_anchored_walk._ROOT_ANCHORS[str(share)] == (here.st_dev, here.st_ino)
    assert level_of(call()) == OTHER_LIBRARY
    assert row_assets._ROW_ASSETS._root == (str(share), here.st_dev, here.st_ino)
    assert platform_paths.resolve_asset_path(ADP).resolved is not None


@pytest.mark.requirement("LIBM-139")
def test_the_explicit_call_refuses_a_symlinked_ancestor_and_says_which(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _nested_share(monkeypatch, tmp_path)
    try:
        assert level_of(call()) == INSIDE
        anchored = dict(fd_anchored_walk._ROOT_ANCHORS)
        _swap_parent_for_a_symlink(root, tmp_path)
        with pytest.raises(fd_anchored_walk.RootReanchorRefused) as refusal:
            fd_anchored_walk.reanchor_root(root)
        assert str(root.parent) in str(refusal.value) and "symbolic link" in str(refusal.value)
        assert anchored == fd_anchored_walk._ROOT_ANCHORS, "a refusal leaves the anchors alone"
        assert level_of(call()) is None, "and the listing still refuses the other tree"
    finally:
        forget_everything()
        fd_anchored_walk.reset_root_anchors()


@pytest.mark.requirement("LIBM-139")
def test_the_explicit_call_accepts_real_ancestors(tmp_path: Path) -> None:
    """Control for the refusal above: the same nested shape, nothing swapped."""
    root = (tmp_path / "crate" / "share").resolve()
    root.mkdir(parents=True)
    try:
        assert fd_anchored_walk.reanchor_root(root) is True
        here = os.stat(root)
        assert fd_anchored_walk._ROOT_ANCHORS[str(root)] == (here.st_dev, here.st_ino)
    finally:
        fd_anchored_walk.reset_root_anchors()


@pytest.mark.requirement("LIBM-139")
def test_the_explicit_call_forgets_every_remembered_row(share: Path) -> None:
    call()
    assert len(row_assets._ROW_ASSETS) == 1
    assert fd_anchored_walk.reanchor_root(share) is True
    assert len(row_assets._ROW_ASSETS) == 0


@pytest.mark.requirement("LIBM-139")
def test_the_explicit_call_on_a_missing_root_changes_nothing(share: Path, tmp_path: Path) -> None:
    call()
    anchored = dict(fd_anchored_walk._ROOT_ANCHORS)
    os.rename(share, tmp_path / "unplugged")
    assert fd_anchored_walk.reanchor_root(share) is False
    assert fd_anchored_walk.reanchor_root(tmp_path / "never" / "there") is False
    assert anchored == fd_anchored_walk._ROOT_ANCHORS
    assert len(row_assets._ROW_ASSETS) == 1


@pytest.mark.requirement("LIBM-139")
def test_the_explicit_call_refuses_a_root_that_is_a_file_or_not_absolute(tmp_path: Path) -> None:
    (tmp_path / "a-file").write_bytes(b"x")
    for bad in (tmp_path / "a-file", tmp_path / "a-file" / "below", Path("relative/share"), tmp_path / ".." / "x"):
        with pytest.raises(fd_anchored_walk.RootReanchorRefused):
            fd_anchored_walk.reanchor_root(bad)
    assert fd_anchored_walk._ROOT_ANCHORS == {}


def _open_descriptors() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.mark.requirement("LIBM-139")
def test_the_explicit_call_leaks_no_descriptor_when_the_root_cannot_be_examined(
    share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault raised by the real ``fstat`` call site, on the opened root only."""
    real_fstat = os.fstat
    before = _open_descriptors()
    assert fd_anchored_walk.reanchor_root(share) is True
    assert _open_descriptors() == before, "control: a successful call holds nothing open"
    wanted = os.stat(share)

    def failing_on_the_root(fd: int) -> os.stat_result:
        got = real_fstat(fd)
        if (got.st_dev, got.st_ino) == (wanted.st_dev, wanted.st_ino):
            raise OSError("fstat failed on the opened root")
        return got

    monkeypatch.setattr(fd_anchored_walk.os, "fstat", failing_on_the_root)
    with pytest.raises(OSError, match="fstat failed"):
        fd_anchored_walk.reanchor_root(share)
    monkeypatch.undo()
    assert _open_descriptors() == before


@pytest.mark.requirement("LIBM-139")
def test_the_explicit_call_moves_the_resolved_form_anchor_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked share root is reached under two names (the link, and what it
    resolves to); each has its own anchor, and both must follow the call."""
    target = (tmp_path / "library").resolve()
    build(target, INSIDE)
    link = tmp_path / "share-link"
    os.symlink(target, link)
    use_root(monkeypatch, link)
    forget_everything()
    try:
        assert level_of(call()) == INSIDE
        fd_anchored_walk.resolve_under_root(target / ANLZ_DIR, target)  # anchors the resolved form
        os.rename(target, tmp_path / "library-unplugged")
        build(target, OTHER_LIBRARY)
        with pytest.raises(fd_anchored_walk.RootIdentityChanged):
            fd_anchored_walk.resolve_under_root(target / ANLZ_DIR, target)
        assert level_of(call()) is None
        assert fd_anchored_walk.reanchor_root(link) is True
        here = os.stat(target)
        assert fd_anchored_walk._ROOT_ANCHORS[str(link)] == (here.st_dev, here.st_ino)
        assert fd_anchored_walk._ROOT_ANCHORS[str(target)] == (here.st_dev, here.st_ino)
        assert fd_anchored_walk.resolve_under_root(target / ANLZ_DIR, target) == target / ANLZ_DIR
        assert level_of(call()) == OTHER_LIBRARY
    finally:
        forget_everything()
        fd_anchored_walk.reset_root_anchors()


@pytest.mark.requirement("LIBM-139")
def test_a_real_root_replaced_by_a_symlink_stays_refused(share: Path, tmp_path: Path) -> None:
    assert level_of(call()) == INSIDE
    build(tmp_path / "attacker", OUTSIDE)
    os.rename(share, tmp_path / "moved-aside")
    os.symlink(tmp_path / "attacker", share)
    refused = call()
    assert level_of(refused) is None
    assert refused.artwork_status == "unresolved"


@pytest.mark.requirement("LIBM-139")
def test_a_symlinked_root_re_pointed_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(tmp_path / "libA", INSIDE)
    build(tmp_path / "libB", OTHER_LIBRARY)
    link = tmp_path / "share-link"
    os.symlink(tmp_path / "libA", link)
    use_root(monkeypatch, link)
    forget_everything()
    try:
        assert level_of(call()) == INSIDE
        os.unlink(link)
        os.symlink(tmp_path / "libB", link)
        for _attempt in range(2):
            assert level_of(call()) is None
    finally:
        forget_everything()
        fd_anchored_walk.reset_root_anchors()


# ----- the two mutations the second review found green ------------------------


def test_a_row_whose_chain_stopped_early_is_a_hit_on_the_next_pass(
    share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The .2EX answers, so the .EXT beside it is never read. It is still
    looked at, or the next pass would see a file the entry never recorded and
    read the row again on every page."""
    (share / ANLZ_DIR / "ANLZ0000.EXT").write_bytes(MALFORMED["garbage"])
    first = call()
    assert level_of(first) == INSIDE
    reads: list[str] = []
    real_read = fd_anchored_walk.read_leaf

    def counting(dir_fd: int, name: str, max_bytes: int):  # type: ignore[no-untyped-def]
        reads.append(name)
        return real_read(dir_fd, name, max_bytes)

    monkeypatch.setattr(fd_anchored_walk, "read_leaf", counting)
    assert call() == first
    assert reads == [], "the remembered row is served without reading a file"
    (share / ANLZ_DIR / "ANLZ0000.EXT").unlink()
    call()
    assert reads, "control: a change to the unread sibling is still a change"


def test_a_missing_share_root_reports_the_cover_missing_not_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_root(monkeypatch, tmp_path / "no-such-share")
    forget_everything()
    try:
        gone = call()
        assert (gone.preview_b64, gone.artwork_available, gone.artwork_status) == (
            None, False, "file_missing",
        )
        assert call(_meta(img=None)).artwork_status == "no_image_path"
    finally:
        fd_anchored_walk.reset_root_anchors()
