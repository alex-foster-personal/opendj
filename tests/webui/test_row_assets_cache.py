"""The listing's row asset cache never serves bytes from outside the share root (LIBM-137).

Round 3 of the listing boot work. Every test here drives the real functions
over a real temporary tree: real symlinks, real permission bits, real FIFOs.
Where a race has to land at one exact instant, the test lets the real call
through and then makes the real filesystem change (the pattern
``tests/shared/test_fd_anchored_walk.py`` uses).

The value under test is identified by its preview level: the share root holds
level 5 (or 9 for a second library), and every file planted OUTSIDE the share
root holds level 13. A 13 on any row is an escape.
"""
from __future__ import annotations

import base64
import errno
import os
import shutil
import struct
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.adapters.rekordbox.models import RbRowMeta
from apps.shared import fd_anchored_walk, platform_paths
from apps.shared.platform_paths import AssetResolver
from apps.webui.server.rb_vendor_pkg import row_assets
from apps.webui.server.rb_vendor_pkg import row_hydration_cache as rhc

pytestmark = pytest.mark.skipif(
    not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED, reason="needs the fd-anchored walk"
)

INSIDE, OTHER_LIBRARY, OUTSIDE = 5, 9, 13
ANLZ_DIR = "PIONEER/USBANLZ/P001/0000A001"
ART_DIR = "PIONEER/Artwork/001/0000A001"
ADP = f"/{ANLZ_DIR}/ANLZ0000.DAT"
IMG = f"/{ART_DIR}/artwork.jpg"


def _meta(adp: str | None = ADP, img: str | None = IMG) -> RbRowMeta:
    return RbRowMeta(
        vendor_id="1", folder_path=None, analysis_data_path=adp,
        comment=None, genre=None, play_count=0, image_path=img,
    )


def _section(fourcc: bytes, head: bytes, payload: bytes) -> bytes:
    head_len = 12 + len(head)
    return fourcc + struct.pack(">II", head_len, head_len + len(payload)) + head + payload


def _pmai(*sections: bytes) -> bytes:
    body = b"".join(sections)
    return b"PMAI" + struct.pack(">II", 28, 28 + len(body)) + bytes(16) + body


def two_ex(level: int) -> bytes:
    preview = _section(b"PWV6", struct.pack(">II", 3, 1200), bytes([level] * 3) * 1200)
    envelope = bytes([3]) * 646
    head = bytes.fromhex("0000040056220001") + struct.pack(">I", len(envelope))
    return _pmai(preview, _section(b"PVDI", head, envelope))


def dat(level: int) -> bytes:
    return _pmai(_section(b"PWAV", struct.pack(">II", 400, 0), bytes([level]) * 400))


def _strip(level: int) -> str:
    return base64.b64encode(bytes([level]) * 360).decode()


def level_of(assets: row_assets.RowAssets) -> int | None:
    if assets.preview_b64 is None:
        return None
    for level in (INSIDE, OTHER_LIBRARY, OUTSIDE):
        if assets.preview_b64 == _strip(level):
            return level
    raise AssertionError("a preview strip that matches no planted level")


def build(share: Path, level: int) -> None:
    anlz = share / ANLZ_DIR
    anlz.mkdir(parents=True)
    (anlz / "ANLZ0000.DAT").write_bytes(dat(level))
    (anlz / "ANLZ0000.2EX").write_bytes(two_ex(level))
    art = share / ART_DIR
    art.mkdir(parents=True)
    (art / "artwork.jpg").write_bytes(b"j")
    (art / "artwork_s.jpg").write_bytes(b"s")


def look_alike_outside(tmp_path: Path) -> Path:
    """A directory OUTSIDE the share root holding level-13 files of the same names."""
    out = tmp_path / "outside" / "0000A001"
    out.mkdir(parents=True)
    (out / "ANLZ0000.DAT").write_bytes(dat(OUTSIDE))
    (out / "ANLZ0000.2EX").write_bytes(two_ex(OUTSIDE))
    return out


def use_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(platform_paths, "SHARE_ROOT", root)
    fd_anchored_walk.reset_root_anchors()


def forget_everything() -> None:
    row_assets._ROW_ASSETS.clear()
    rhc._PREVIEW_CACHE.clear()
    rhc._VOCALS_CACHE.clear()


def call(meta: RbRowMeta | None = None) -> row_assets.RowAssets:
    return row_assets.rb_row_assets(meta or _meta(), resolver=AssetResolver(), stable_id="sid-under-test")


@pytest.fixture
def share(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    root = (tmp_path / "share").resolve()
    build(root, INSIDE)
    use_root(monkeypatch, root)
    forget_everything()
    yield root
    forget_everything()
    fd_anchored_walk.reset_root_anchors()


def test_the_fixture_row_reads_and_is_remembered(share: Path) -> None:
    first = call()
    assert level_of(first) == INSIDE
    assert first.pvdi_vocals["status"] == "rekordbox"
    assert (first.artwork_available, first.artwork_status) == (True, "ok")
    assert len(row_assets._ROW_ASSETS) == 1
    assert call() == first


# ----- P1-1: an error while re-checking a remembered row degrades that row ----


def test_directory_turned_symlink_loop_degrades_the_row(share: Path) -> None:
    assert level_of(call()) == INSIDE
    directory = share / ANLZ_DIR
    shutil.rmtree(directory)
    os.symlink(directory, directory)  # resolving it is ELOOP
    with pytest.raises(OSError) as loop:
        os.stat(directory)
    assert loop.value.errno == errno.ELOOP
    warm = call()  # must not raise: one row degrades, the page survives
    assert level_of(warm) is None
    assert warm.pvdi_vocals == {"status": "not_analyzed"}
    assert warm.artwork_status == "ok", "the artwork half of the row is untouched"
    assert len(row_assets._ROW_ASSETS) == 0, "an errored row is not remembered"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_unreadable_directory_degrades_the_row_and_recovers(share: Path) -> None:
    assert level_of(call()) == INSIDE
    parent = share / "PIONEER/USBANLZ/P001"
    os.chmod(parent, 0)
    try:
        with pytest.raises(PermissionError):
            os.stat(share / ANLZ_DIR)
        warm = call()
        assert level_of(warm) is None
        assert len(row_assets._ROW_ASSETS) == 0
    finally:
        os.chmod(parent, 0o755)
    assert level_of(call()) == INSIDE, "a refused row reads again once it is readable"
    assert len(row_assets._ROW_ASSETS) == 1


def test_overlong_component_degrades_the_row(share: Path) -> None:
    meta = _meta(adp=f"/PIONEER/USBANLZ/{'x' * 300}/ANLZ0000.DAT")
    for _attempt in ("cold", "warm"):
        assets = call(meta)  # ENAMETOOLONG on every open and every lstat
        assert level_of(assets) is None
        assert assets.artwork_status == "ok"
    assert len(row_assets._ROW_ASSETS) == 0


def test_io_error_on_the_recheck_degrades_the_row(
    share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A volume that goes away answers EIO. That cannot be produced for real
    here, so this one test injects the errno into every ``lstat`` and ``open``
    of one file; ELOOP, EACCES and ENAMETOOLONG above are the real thing."""
    assert level_of(call()) == INSIDE
    real_stat, real_open = os.stat, os.open

    def stat_on_a_dead_volume(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path).endswith(".2EX"):
            raise OSError(errno.EIO, "Input/output error")
        return real_stat(path, *args, **kwargs)

    def open_on_a_dead_volume(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path).endswith(".2EX"):
            raise OSError(errno.EIO, "Input/output error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat_on_a_dead_volume)
    monkeypatch.setattr(os, "open", open_on_a_dead_volume)
    warm = call()
    assert level_of(warm) == INSIDE, "the .DAT fallback is still readable"
    assert warm.pvdi_vocals == {"status": "not_analyzed"}
    assert len(row_assets._ROW_ASSETS) == 0


# ----- P1-2: a won race is neither served nor remembered ---------------------


def test_swap_during_the_read_never_serves_or_remembers_outside_bytes(
    share: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The analysis directory is swapped for a symlink to outside look-alikes at
    the instant the walk has opened it. Whatever that request answers, it is not
    the outside bytes, and no later request answers them either."""
    out = look_alike_outside(tmp_path)
    directory = share / ANLZ_DIR
    real_open = os.open
    swapped: list[bool] = []

    def open_then_swap(path, flags, mode=0o777, *, dir_fd=None):  # type: ignore[no-untyped-def]
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if not swapped and dir_fd is not None and os.fspath(path) == "0000A001":
            swapped.append(True)
            os.rename(directory, tmp_path / "parked")
            os.symlink(out, directory)
        return fd

    monkeypatch.setattr(fd_anchored_walk.os, "open", open_then_swap)
    raced = call()
    assert swapped, "the swap never fired: this test would prove nothing"
    assert level_of(raced) != OUTSIDE
    monkeypatch.setattr(fd_anchored_walk.os, "open", real_open)
    for _later in range(3):
        assert level_of(call()) is None, "the path is a symlink out of the root now"
    # ...and undoing the swap must not resurrect anything derived during it.
    os.unlink(directory)
    os.rename(tmp_path / "parked", directory)
    assert level_of(call()) == INSIDE


@pytest.mark.parametrize("when", ["before the open", "after the open"])
def test_leaf_swapped_at_the_instant_it_is_opened(
    share: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """The .2EX becomes a symlink to an outside look-alike just before, or just
    after, the read opens it; then the swap is undone (the ABA shape). Before:
    the open refuses the symlink. After: the bytes come from the descriptor
    that was already open on the real file. Either way nothing outside is
    served, then or on any later request."""
    out = look_alike_outside(tmp_path)
    leaf = share / ANLZ_DIR / "ANLZ0000.2EX"
    parked = tmp_path / "parked.2EX"
    real_open = os.open
    swapped: list[bool] = []

    def swap() -> None:
        swapped.append(True)
        os.rename(leaf, parked)
        os.symlink(out / "ANLZ0000.2EX", leaf)

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):  # type: ignore[no-untyped-def]
        mine = not swapped and dir_fd is not None and os.fspath(path) == "ANLZ0000.2EX"
        if mine and when == "before the open":
            swap()
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if mine and when == "after the open":
            swap()
        return fd

    monkeypatch.setattr(fd_anchored_walk.os, "open", racing_open)
    raced = call()
    assert swapped, "the swap never fired: this test would prove nothing"
    assert level_of(raced) == INSIDE
    monkeypatch.setattr(fd_anchored_walk.os, "open", real_open)
    assert level_of(call()) == INSIDE, "the symlink is refused; the .DAT is the fallback"
    assert call().pvdi_vocals == {"status": "not_analyzed"}
    os.unlink(leaf)
    os.rename(parked, leaf)  # the swap is undone
    for _later in range(2):
        restored = call()
        assert level_of(restored) == INSIDE
        assert restored.pvdi_vocals["status"] == "rekordbox"


def test_leaf_symlink_to_an_outside_look_alike_is_refused_and_not_remembered(
    share: Path, tmp_path: Path
) -> None:
    out = look_alike_outside(tmp_path)
    leaf = share / ANLZ_DIR / "ANLZ0000.2EX"
    original = leaf.read_bytes()
    leaf.unlink()
    os.symlink(out / "ANLZ0000.2EX", leaf)
    for _attempt in ("cold", "again"):
        assets = call()
        assert level_of(assets) == INSIDE, "the .DAT beside it is the fallback"
        assert assets.pvdi_vocals == {"status": "not_analyzed"}
        assert len(row_assets._ROW_ASSETS) == 0, "a row with a refused file is not remembered"
    leaf.unlink()
    leaf.write_bytes(original)
    assert call().pvdi_vocals["status"] == "rekordbox", "the refused row recovers"
    assert len(row_assets._ROW_ASSETS) == 1


def test_remembered_leaf_swapped_for_a_symlink_is_a_miss(share: Path, tmp_path: Path) -> None:
    out = look_alike_outside(tmp_path)
    assert level_of(call()) == INSIDE
    for name in ("ANLZ0000.2EX", "ANLZ0000.DAT"):
        leaf = share / ANLZ_DIR / name
        leaf.unlink()
        os.symlink(out / name, leaf)
    assert level_of(call()) is None
    assert len(row_assets._ROW_ASSETS) == 0


def test_vendor_leaf_that_is_a_symlink_refuses_the_row_as_the_uncached_path_does(
    share: Path, tmp_path: Path
) -> None:
    """The file the vendor path names is a symlink; the .2EX beside it is real.
    The uncached resolver refuses the whole analysis path, so this does too."""
    out = look_alike_outside(tmp_path)
    leaf = share / ANLZ_DIR / "ANLZ0000.DAT"
    leaf.unlink()
    os.symlink(out / "ANLZ0000.DAT", leaf)
    from apps.webui.server.rb_vendor_pkg import anlz

    assert anlz.preview_strip(ADP, resolver=AssetResolver()) == (None, None)
    assets = call()
    assert level_of(assets) is None
    assert assets.pvdi_vocals == {"status": "not_analyzed"}
    assert len(row_assets._ROW_ASSETS) == 0


def test_parent_reference_in_the_vendor_path_never_reaches_outside(
    share: Path, tmp_path: Path
) -> None:
    out = share.parent / "outside" / "0000A001"
    look_alike_outside(tmp_path)
    assert (out / "ANLZ0000.2EX").is_file()
    meta = _meta(adp="/PIONEER/../../outside/0000A001/ANLZ0000.DAT")
    for _attempt in ("cold", "warm"):
        assert level_of(call(meta)) is None
    assert len(row_assets._ROW_ASSETS) == 0


def test_fifo_in_place_of_an_analysis_file_is_not_read(share: Path) -> None:
    leaf = share / ANLZ_DIR / "ANLZ0000.2EX"
    leaf.unlink()
    os.mkfifo(leaf)
    assets = call()  # a blocking open here would hang the listing
    assert level_of(assets) == INSIDE
    assert assets.pvdi_vocals == {"status": "not_analyzed"}


# ----- P2-4: a directory moved out with a symlink left behind ----------------


def test_original_moved_outside_with_a_symlink_left_is_refused(
    share: Path, tmp_path: Path
) -> None:
    """Same inodes, same files, but the path now leaves the root: the live walk
    says unsafe, so the remembered row must not be served."""
    assert level_of(call()) == INSIDE
    parent = share / "PIONEER/USBANLZ/P001"
    out = tmp_path / "moved-out"
    out.mkdir()
    shutil.move(str(parent), str(out / "P001"))
    os.symlink(out / "P001", parent)
    assert platform_paths.resolve_asset_path(ADP).resolved is None, "the live walk refuses it"
    assert level_of(call()) is None
    assert len(row_assets._ROW_ASSETS) == 0


# ----- P2-3: the cache belongs to one share root ----------------------------


def test_another_root_with_the_same_vendor_strings_is_read_afresh(
    share: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert level_of(call()) == INSIDE
    other = (tmp_path / "other-library").resolve()
    build(other, OTHER_LIBRARY)
    # No anchor reset here: that would empty the cache and hide what is under
    # test, which is that an entry answers only for the root it was read under.
    monkeypatch.setattr(platform_paths, "SHARE_ROOT", other)
    assert len(row_assets._ROW_ASSETS) == 1
    assert level_of(call()) == OTHER_LIBRARY


def test_the_cache_is_bound_to_the_root_s_path_and_identity(share: Path) -> None:
    """Below the root every directory and file is witnessed, so a different
    root shows as a miss there too. The binding is the second lock: it is what
    empties the cache, and it is pinned here on its own."""
    call()
    anchored = os.stat(share)
    assert row_assets._ROW_ASSETS._root == (str(share), anchored.st_dev, anchored.st_ino)


def test_same_root_path_naming_another_directory_is_read_afresh(
    share: Path, tmp_path: Path
) -> None:
    """The root's path string is unchanged; the directory it names is not."""
    assert level_of(call()) == INSIDE
    os.rename(share, tmp_path / "old-library")
    build(share, OTHER_LIBRARY)
    fd_anchored_walk.reset_root_anchor(share)  # this one root, cache untouched
    assert len(row_assets._ROW_ASSETS) == 1
    assert level_of(call()) == OTHER_LIBRARY


def test_symlinked_root_repointed_serves_nothing_until_refreshed(
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
        assert level_of(call()) is None, "a root that changed identity is refused, not served"
        monkeypatch.setattr(platform_paths, "compute_share_root", lambda: link)
        platform_paths.refresh_share_root()
        assert len(row_assets._ROW_ASSETS) == 0, "refresh_share_root forgets every row"
        assert level_of(call()) == OTHER_LIBRARY
    finally:
        forget_everything()
        fd_anchored_walk.reset_root_anchors()


# ----- what counts as "unchanged" --------------------------------------------


def test_same_size_rewrite_with_the_mtime_put_back_shows(share: Path) -> None:
    assert level_of(call()) == INSIDE
    leaf = share / ANLZ_DIR / "ANLZ0000.2EX"
    before = os.stat(leaf)
    # The kernel stamps ctime from a coarse clock (one jiffy, up to 4 ms on
    # ext4), so a rewrite inside the same tick as the fixture's own write keeps
    # ctime too and is genuinely indistinguishable. A real rewrite comes later;
    # wait the clock past the fixture's ctime so this models one (CI hit it).
    deadline = time.monotonic() + 2.0
    while time.time_ns() <= before.st_ctime_ns + 20_000_000 and time.monotonic() < deadline:
        time.sleep(0.005)
    with open(leaf, "r+b") as handle:
        handle.write(two_ex(OTHER_LIBRARY))
    os.utime(leaf, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(leaf)
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (
        before.st_size, before.st_mtime_ns, before.st_ino,
    ), "the fixture did not produce a same-size same-mtime same-inode rewrite"
    assert after.st_ctime_ns != before.st_ctime_ns, "ctime did not move; the rewrite is unobservable"
    assert level_of(call()) == OTHER_LIBRARY


def test_file_appearing_earlier_in_the_chain_shows(share: Path) -> None:
    (share / ANLZ_DIR / "ANLZ0000.2EX").unlink()
    assert call().pvdi_vocals == {"status": "not_analyzed"}
    (share / ANLZ_DIR / "ANLZ0000.2EX").write_bytes(two_ex(OTHER_LIBRARY))
    assert level_of(call()) == OTHER_LIBRARY


def test_unrelated_file_in_the_directory_keeps_the_row_remembered(
    share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overshoot: a directory's own times move whenever anything in it
    does, so witnessing them would turn every hit into a read."""
    assert level_of(call()) == INSIDE
    (share / ANLZ_DIR / "unrelated.tmp").write_bytes(b"x")
    reads: list[str] = []
    real_read_leaf = fd_anchored_walk.read_leaf

    def counting_read_leaf(dir_fd: int, name: str, max_bytes: int):  # type: ignore[no-untyped-def]
        reads.append(name)
        return real_read_leaf(dir_fd, name, max_bytes)

    monkeypatch.setattr(fd_anchored_walk, "read_leaf", counting_read_leaf)
    assert level_of(call()) == INSIDE
    assert reads == [], "a remembered, unchanged row reads no file"


# ----- what a caller can do to a remembered row ------------------------------


def test_mutating_returned_vocals_does_not_reach_the_cache(share: Path) -> None:
    first = call()
    pristine = call().pvdi_vocals
    first.pvdi_vocals["regions"].clear()
    first.pvdi_vocals["status"] = "tampered"
    assert call().pvdi_vocals == pristine
    assert pristine["status"] == "rekordbox" and pristine["regions"]


# ----- what is never remembered ---------------------------------------------


def test_a_path_outside_the_share_rule_is_not_remembered(share: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "ANLZ0000.DAT").write_bytes(dat(OUTSIDE))
    call(_meta(adp=str(elsewhere / "ANLZ0000.DAT"), img=None))
    assert len(row_assets._ROW_ASSETS) == 0


# ----- P3: the cover itself is contained ------------------------------------


def test_cover_that_is_a_symlink_is_unresolved_and_not_remembered(
    share: Path, tmp_path: Path
) -> None:
    cover = share / ART_DIR / "artwork_s.jpg"
    cover.unlink()
    target = tmp_path / "elsewhere.jpg"
    target.write_bytes(b"x")
    os.symlink(target, cover)
    for _attempt in ("cold", "again"):
        assets = call()
        assert (assets.artwork_available, assets.artwork_status) == (False, "unresolved")
        assert len(row_assets._ROW_ASSETS) == 0
    assert row_assets.rb_artwork_facts(_meta()) == (False, "unresolved"), "the uncached path agrees"


def test_remembered_cover_swapped_for_a_symlink_is_unresolved(share: Path, tmp_path: Path) -> None:
    assert call().artwork_status == "ok"
    cover = share / ART_DIR / "artwork_s.jpg"
    target = tmp_path / "elsewhere.jpg"
    target.write_bytes(b"s")
    cover.unlink()
    os.symlink(target, cover)
    assert call().artwork_status == "unresolved"


# ----- no descriptor outlives a call -----------------------------------------


def test_no_descriptor_is_left_open(share: Path, tmp_path: Path) -> None:
    out = look_alike_outside(tmp_path)

    def open_descriptors() -> int:
        return len(os.listdir("/dev/fd"))

    before = open_descriptors()
    for _round in range(5):
        call()  # hit or cold read
        forget_everything()
        call(_meta(adp="/PIONEER/USBANLZ/P999/NOPE/ANLZ0000.DAT"))  # missing directory
    leaf = share / ANLZ_DIR / "ANLZ0000.2EX"
    leaf.unlink()
    os.symlink(out / "ANLZ0000.2EX", leaf)
    for _round in range(5):
        call()  # refused leaf
    assert open_descriptors() == before
