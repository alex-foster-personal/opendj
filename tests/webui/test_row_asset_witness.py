"""Unit pins for the row asset cache's witnesses and its bound (LIBM-137 round 3)."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.shared.fd_anchored_walk import identity_of
from apps.webui.server.rb_vendor_pkg import row_assets
from apps.webui.server.rb_vendor_pkg import row_hydration_cache as rhc

FIELDS = ("st_mode", "st_ino", "st_dev", "st_size", "st_mtime_ns", "st_ctime_ns")
ROOT: rhc.RootKey = ("/share", 1, 2)
VALUE: rhc.RowValue = (None, None, False, "no_image_path", '{"status": "not_analyzed"}')


def _like(st: os.stat_result, **changed: int) -> os.stat_result:
    """A stat result equal to a REAL one except for the named fields."""
    fields = {name: getattr(st, name) for name in FIELDS} | changed
    return SimpleNamespace(**fields)  # type: ignore[return-value]


@pytest.fixture
def real_file(tmp_path: Path) -> os.stat_result:
    path = tmp_path / "a-file"
    path.write_bytes(b"0123456789")
    return os.lstat(path)


def test_identity_is_the_six_fields_of_the_lstat(real_file: os.stat_result) -> None:
    assert identity_of(real_file) == tuple(getattr(real_file, name) for name in FIELDS)


@pytest.mark.parametrize("field", FIELDS)
def test_file_witness_moves_with_every_field(real_file: os.stat_result, field: str) -> None:
    base = rhc.file_witness(real_file)
    assert rhc.file_witness(_like(real_file)) == base, "the stand-in reproduces the real witness"
    moved = getattr(real_file, field) + (stat.S_IFLNK if field == "st_mode" else 1)
    assert rhc.file_witness(_like(real_file, **{field: moved})) != base


def test_look_alike_with_the_same_size_and_mtime_is_a_different_file(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.write_bytes(b"0123456789")
    two.write_bytes(b"9876543210")
    first = os.lstat(one)
    os.utime(two, ns=(first.st_atime_ns, first.st_mtime_ns))
    second = os.lstat(two)
    assert (second.st_size, second.st_mtime_ns) == (first.st_size, first.st_mtime_ns)
    same_but_for_inode = _like(first, st_ino=second.st_ino)
    assert rhc.file_witness(same_but_for_inode) != rhc.file_witness(first)


@pytest.mark.parametrize("field", ("st_ino", "st_dev"))
def test_directory_witness_moves_with_identity(tmp_path: Path, field: str) -> None:
    st = os.lstat(tmp_path)
    moved = _like(st, **{field: getattr(st, field) + 1})
    assert rhc.directory_witness(moved) != rhc.directory_witness(st)


def test_directory_witness_tells_a_symlink_from_the_directory_it_points_at(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    followed = os.stat(link)
    assert (followed.st_ino, followed.st_dev) == (os.lstat(real).st_ino, os.lstat(real).st_dev)
    assert rhc.directory_witness(os.lstat(link)) != rhc.directory_witness(os.lstat(real))
    as_symlink = _like(os.lstat(real), st_mode=stat.S_IFLNK | 0o755)
    assert rhc.directory_witness(as_symlink) != rhc.directory_witness(os.lstat(real))


def test_directory_witness_ignores_the_directory_s_own_times(tmp_path: Path) -> None:
    before = os.lstat(tmp_path)
    (tmp_path / "new-entry").write_bytes(b"x")
    after = os.lstat(tmp_path)
    assert after.st_mtime_ns != before.st_mtime_ns or after.st_size != before.st_size
    assert rhc.directory_witness(after) == rhc.directory_witness(before)


def test_lstat_below_does_not_follow_the_last_symlink(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    os.symlink(tmp_path / "real", tmp_path / "link")
    root_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        seen = rhc.lstat_below(root_fd, "link")
        assert isinstance(seen, os.stat_result) and stat.S_ISLNK(seen.st_mode)
        assert rhc.lstat_below(root_fd, "nothing-here") is None
        assert rhc.lstat_below(root_fd, "link/nothing-here") is None
        os.symlink(tmp_path / "loop", tmp_path / "loop")
        assert isinstance(rhc.lstat_below(root_fd, "loop/x"), OSError)
    finally:
        os.close(root_fd)


def test_digest_moves_with_any_witness_and_with_their_order() -> None:
    a, b = bytes([1]) * 48, bytes([2]) * 48
    assert rhc.digest([a, b]) == rhc.digest([a, b])
    assert rhc.digest([a, b]) != rhc.digest([b, a])
    assert rhc.digest([a, b]) != rhc.digest([a, rhc._ABSENT])
    assert len(rhc.digest([a, b])) == 16


# ----- the cache itself ------------------------------------------------------


def test_cache_is_bounded_and_evicts_the_least_recently_used() -> None:
    cache = rhc.RowAssetCache(max_entries=2)
    cache.put(ROOT, ("a", ""), b"w", VALUE)
    cache.put(ROOT, ("b", ""), b"w", VALUE)
    assert cache.get(ROOT, ("a", "")) is not None  # "a" is now the most recent
    cache.put(ROOT, ("c", ""), b"w", VALUE)
    assert len(cache) == 2
    assert cache.get(ROOT, ("b", "")) is None
    assert cache.get(ROOT, ("a", "")) is not None
    assert cache.get(ROOT, ("c", "")) is not None


def test_the_shipped_cap_is_stated_and_finite() -> None:
    assert row_assets.ROW_ASSET_CACHE_MAX_ENTRIES == 40_000
    assert row_assets._ROW_ASSETS._max_entries == row_assets.ROW_ASSET_CACHE_MAX_ENTRIES


@pytest.mark.parametrize(
    "other", [("/elsewhere", 1, 2), ("/share", 9, 2), ("/share", 1, 9)]
)
def test_cache_answers_only_for_the_root_it_was_filled_under(other: rhc.RootKey) -> None:
    cache = rhc.RowAssetCache(max_entries=10)
    cache.put(ROOT, ("a", ""), b"w", VALUE)
    assert cache.get(other, ("a", "")) is None
    cache.discard(other, ("a", ""))
    assert cache.get(ROOT, ("a", "")) is not None, "another root's discard leaves this one alone"
    cache.put(other, ("b", ""), b"w", VALUE)
    assert cache.get(ROOT, ("a", "")) is None, "filling under a new root drops the old root's rows"
    assert len(cache) == 1


def test_discard_and_clear() -> None:
    cache = rhc.RowAssetCache(max_entries=10)
    cache.put(ROOT, ("a", ""), b"w", VALUE)
    cache.discard(ROOT, ("a", ""))
    assert len(cache) == 0
    cache.put(ROOT, ("a", ""), b"w", VALUE)
    cache.clear()
    assert cache.get(ROOT, ("a", "")) is None


# ----- which rows may take the remembered path at all ------------------------


@pytest.mark.parametrize(
    "vendor_path",
    [
        "/PIONEER/../outside/ANLZ0000.DAT",
        "/PIONEER/./USBANLZ/ANLZ0000.DAT",
        "/PIONEER//USBANLZ/ANLZ0000.DAT",
        "/PIONEER/USBANLZ/",
        "/PIONEER/USB\x00ANLZ/ANLZ0000.DAT",
        "/Users/someone/ANLZ0000.DAT",
        "PIONEER/USBANLZ/ANLZ0000.DAT",
        "C:\\PIONEER\\USBANLZ\\ANLZ0000.DAT",
    ],
)
def test_only_plain_share_relative_paths_are_planned(vendor_path: str) -> None:
    assert row_assets._share_parts(vendor_path) is None


def test_a_plain_share_relative_path_is_planned() -> None:
    assert row_assets._share_parts("/PIONEER/USBANLZ/P001/0000A001/ANLZ0000.DAT") == (
        "PIONEER", "USBANLZ", "P001", "0000A001", "ANLZ0000.DAT",
    )
