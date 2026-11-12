"""Tests for restoring a USB stick snapshot with a byte comparison (issue #12230).

The snapshots here are synthesized in this file in the #12229 snapshot manifest
format (a tree plus ``<tree>.manifest.json`` beside it); no fixture files are
read or changed. Restore itself runs for real against temporary directories.

[if] a snapshot restores [then] the receipt proves a byte-exact copy, [else stop].
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.sync.usb import __main__ as cli
from apps.sync.usb import snapshot as snap
from apps.sync.usb import snapshot_restore as sr

pytestmark = pytest.mark.requirement("USBEXP-12737")

REPO_ROOT = Path(__file__).resolve().parents[3]

# Synthesized stick content: nested audio, analysis and metadata bytes.
SNAP_FILES: dict[str, bytes] = {
    "Contents/Artist A/Album 1/01 Opener.mp3": b"ID3\x04" + bytes(range(256)) * 3,
    "Contents/Artist A/Album 1/02 Closer.flac": b"fLaC" + b"\x7f" * 90,
    "Contents/Artist B/Edit.wav": b"RIFF" + b"\x10" * 40 + b"WAVE",
    "PIONEER/rekordbox/export.pdb": b"\x00\x00\x00\x00pdb",
    "PIONEER/USBANLZ/P01A/0000BEEF/ANLZ0000.DAT": b"PMAI" + b"\x05" * 24,
    "PIONEER/empty.txt": b"",
}
EXTRA_DIRS = ("PIONEER/Artwork",)

posix_permissions = pytest.mark.skipif(
    os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX file permissions as a non-root user (Windows chmod and root ignore them)",
)


def _all_dirs(files: dict[str, bytes], extra: tuple[str, ...]) -> list[str]:
    dirs = set(extra)
    for rel in [*files, *extra]:
        parts = rel.split("/")[:-1] if rel in files else rel.split("/")
        dirs.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return sorted(dirs)


def write_manifest(snapshot: Path, dirs: list[str], files: list[dict]) -> Path:
    body = {"schema": snap.MANIFEST_SCHEMA, "dirs": dirs, "files": files, "unsupported": []}
    path = snap.manifest_path_for(snapshot)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return path


def make_snapshot(base: Path, name: str = "SNAP-SOURCE") -> Path:
    """Build a snapshot tree and its manifest under ``base``."""
    snapshot = base / name
    for d in _all_dirs(SNAP_FILES, EXTRA_DIRS):
        (snapshot / d).mkdir(parents=True, exist_ok=True)
    entries = []
    for rel, data in sorted(SNAP_FILES.items()):
        (snapshot / rel).write_bytes(data)
        entries.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    write_manifest(snapshot, _all_dirs(SNAP_FILES, EXTRA_DIRS), entries)
    return snapshot


def edit_manifest(snapshot: Path, fn) -> None:
    path = snap.manifest_path_for(snapshot)
    data = json.loads(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def tree_bytes(root: Path) -> dict[str, bytes | None]:
    """Independent recursive read of a tree: file bytes, or None for a dir."""
    out: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = None if path.is_dir() else path.read_bytes()
    return out


def everything_under(base: Path) -> set[str]:
    return {p.relative_to(base).as_posix() for p in base.rglob("*")}


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    return make_snapshot(tmp_path / "work")


@pytest.mark.parametrize("existing_empty_root", [False, True])
def test_valid_snapshot_restores_byte_identical(snapshot: Path, tmp_path: Path, existing_empty_root: bool) -> None:
    """[if] a valid snapshot restores to an empty root [then] every byte matches, [else stop]."""
    dest = tmp_path / "RESTORE-TARGET"
    if existing_empty_root:
        dest.mkdir()
    receipt = sr.restore_snapshot(snapshot, dest)

    assert tree_bytes(dest) == tree_bytes(snapshot)
    assert receipt.ok and receipt.byte_compare.ok
    assert receipt.files == len(SNAP_FILES)
    assert receipt.bytes == sum(len(b) for b in SNAP_FILES.values())
    assert receipt.byte_compare.files_compared == len(SNAP_FILES)
    assert receipt.byte_compare.bytes_compared == receipt.bytes
    for rel in SNAP_FILES:
        assert (dest / rel).stat().st_ino != (snapshot / rel).stat().st_ino, "restore copies, never links"


def _same_size_flip(snap: Path) -> None:
    target = snap / "PIONEER/rekordbox/export.pdb"
    target.write_bytes(target.read_bytes()[:-1] + b"X")


MISMATCHES = {
    "same_size_byte_change": _same_size_flip,
    "truncated": lambda s: (s / "Contents/Artist B/Edit.wav").write_bytes(b"RIFF"),
    "missing_file": lambda s: (s / "PIONEER/empty.txt").unlink(),
    "extra_file": lambda s: (s / "Contents/stray.mp3").write_bytes(b"stray"),
    "extra_dir": lambda s: (s / "Contents/Stray Dir").mkdir(),
    "missing_dir": lambda s: (s / "PIONEER/Artwork").rmdir(),
    "tampered_digest": lambda s: edit_manifest(s, lambda d: d["files"][0].update(sha256="0" * 64)),
    "tampered_size": lambda s: edit_manifest(s, lambda d: d["files"][1].update(size=1)),
}


@pytest.mark.parametrize("mutation", sorted(MISMATCHES))
@pytest.mark.parametrize("existing_empty_root", [False, True])
def test_manifest_mismatch_writes_nothing(
    snapshot: Path, tmp_path: Path, mutation: str, existing_empty_root: bool
) -> None:
    """[if] the snapshot mismatches its manifest [then] no destination file is written, [else stop]."""
    MISMATCHES[mutation](snapshot)
    dest = tmp_path / "RESTORE-TARGET"
    if existing_empty_root:
        dest.mkdir()
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "manifest_mismatch"
    assert exc.value.entries
    if existing_empty_root:
        assert list(dest.iterdir()) == []
    else:
        assert not os.path.lexists(dest)


def test_symlink_in_snapshot_is_a_mismatch(snapshot: Path, tmp_path: Path) -> None:
    """[if] a snapshot file is swapped for a symlink [then] restore refuses and writes nothing, [else stop]."""
    target = snapshot / "PIONEER/rekordbox/export.pdb"
    target.unlink()
    try:
        target.symlink_to(snapshot / "PIONEER/empty.txt")
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"this account cannot create symlinks: {exc}")
    dest = tmp_path / "RESTORE-TARGET"
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "manifest_mismatch"
    assert not os.path.lexists(dest)


ESCAPES = [
    "../outside.mp3",
    "Contents/../../outside.mp3",
    "/outside.mp3",
    "C:/outside.mp3",
    "Contents\\..\\..\\outside.mp3",
    "./Contents/x.mp3",
    "Contents//x.mp3",
]


@pytest.mark.parametrize("bad_path", ESCAPES)
def test_escaping_path_is_refused_before_any_write(snapshot: Path, tmp_path: Path, bad_path: str) -> None:
    """[if] a manifest path would leave the restore root [then] nothing is written anywhere, [else stop]."""
    entry = {"path": bad_path, "sha256": hashlib.sha256(b"evil").hexdigest(), "size": 4}
    edit_manifest(snapshot, lambda d: d["files"].append(entry))
    before = everything_under(tmp_path)
    dest = tmp_path / "jail" / "RESTORE-TARGET"
    dest.parent.mkdir()
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "unsafe_path"
    assert [e["path"] for e in exc.value.entries] == [bad_path]
    assert everything_under(tmp_path) == before | {"jail"}
    assert not (tmp_path / "outside.mp3").exists() and not (tmp_path / "jail" / "outside.mp3").exists()


def test_restore_root_symlink_is_refused(snapshot: Path, tmp_path: Path) -> None:
    """[if] the restore root is a symlink [then] restore refuses and writes through nothing, [else stop]."""
    elsewhere = tmp_path / "ELSEWHERE"
    elsewhere.mkdir()
    link = tmp_path / "RESTORE-LINK"
    try:
        link.symlink_to(elsewhere, target_is_directory=True)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"this account cannot create symlinks: {exc}")
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, link)
    assert exc.value.code == "destination_is_symlink"
    assert list(elsewhere.iterdir()) == []


def test_case_folded_collision_is_refused(snapshot: Path, tmp_path: Path) -> None:
    """[if] two manifest paths differ only by case [then] restore refuses, as FAT would collide, [else stop]."""
    upper = "PIONEER/EMPTY.TXT"  # manifest only: the collision is refused before the tree is read
    entry = {"path": upper, "sha256": hashlib.sha256(b"").hexdigest(), "size": 0}
    edit_manifest(snapshot, lambda d: d["files"].append(entry))
    dest = tmp_path / "RESTORE-TARGET"
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "path_collision"
    assert not os.path.lexists(dest)


def test_unsupported_entries_are_listed_as_not_restored(snapshot: Path, tmp_path: Path) -> None:
    """[if] the manifest lists unsupported entries [then] the receipt names them as not restored, [else stop]."""
    skipped = {"kind": "symlink", "path": "Contents/link.mp3"}
    edit_manifest(snapshot, lambda d: d.update(unsupported=[skipped]))
    dest = tmp_path / "RESTORE-TARGET"
    receipt = sr.restore_snapshot(snapshot, dest)
    assert receipt.not_restored == [skipped]
    assert not os.path.lexists(dest / "Contents/link.mp3")


def test_non_empty_root_is_never_overwritten(snapshot: Path, tmp_path: Path) -> None:
    """[if] the restore root already holds a file [then] restore refuses and leaves it as is, [else stop]."""
    dest = tmp_path / "RESTORE-TARGET"
    (dest / "PIONEER/rekordbox").mkdir(parents=True)
    keep = dest / "PIONEER/rekordbox/export.pdb"
    keep.write_bytes(b"user data")
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "destination_not_empty"
    assert keep.read_bytes() == b"user data"
    assert everything_under(dest) == {"PIONEER", "PIONEER/rekordbox", "PIONEER/rekordbox/export.pdb"}


def test_invalid_manifest_is_refused(snapshot: Path, tmp_path: Path) -> None:
    """[if] the manifest has the wrong schema or a bad entry [then] restore writes nothing, [else stop]."""
    dest = tmp_path / "RESTORE-TARGET"
    edit_manifest(snapshot, lambda d: d["files"][0].pop("size"))
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "manifest_invalid"
    edit_manifest(snapshot, lambda d: d.update(schema="something-else/9"))
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "manifest_invalid"
    snap.manifest_path_for(snapshot).unlink()
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "manifest_unreadable"
    assert not os.path.lexists(dest)


@posix_permissions
def test_unreadable_snapshot_file_fails_closed(snapshot: Path, tmp_path: Path) -> None:
    """[if] a snapshot file cannot be read [then] restore fails closed and writes nothing, [else stop]."""
    locked = snapshot / "Contents/Artist B/Edit.wav"
    locked.chmod(0)
    try:
        dest = tmp_path / "RESTORE-TARGET"
        with pytest.raises(sr.RestoreError) as exc:
            sr.restore_snapshot(snapshot, dest)
        assert exc.value.code == "snapshot_unreadable"
        assert not os.path.lexists(dest)
    finally:
        locked.chmod(0o644)


@posix_permissions
def test_unwritable_root_fails_closed(snapshot: Path, tmp_path: Path) -> None:
    """[if] the restore root cannot be written [then] restore fails and leaves no partial tree, [else stop]."""
    dest = tmp_path / "RESTORE-TARGET"
    dest.mkdir()
    dest.chmod(0o555)
    try:
        with pytest.raises(sr.RestoreError) as exc:
            sr.restore_snapshot(snapshot, dest)
        assert exc.value.code == "write_failed"
        assert list(dest.iterdir()) == []
    finally:
        dest.chmod(0o755)


def test_failure_mid_restore_rolls_back_only_its_own_files(
    snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a write fails part way [then] every file this run made is removed, [else stop]."""
    calls = {"n": 0}
    real_fsync = os.fsync

    def failing_fsync(fd: int) -> None:  # the double stands in for the OS, not for restore
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        real_fsync(fd)

    monkeypatch.setattr(sr.os, "fsync", failing_fsync)
    dest = tmp_path / "RESTORE-TARGET"
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "write_failed"
    assert not os.path.lexists(dest)
    assert tree_bytes(snapshot) == {**{d: None for d in _all_dirs(SNAP_FILES, EXTRA_DIRS)}, **SNAP_FILES}


def test_byte_compare_reports_every_difference(snapshot: Path, tmp_path: Path) -> None:
    """[if] a restored tree differs by one byte, a file or a dir [then] the compare fails, [else stop]."""
    dest = tmp_path / "RESTORE-TARGET"
    sr.restore_snapshot(snapshot, dest)
    assert sr.compare_trees(snapshot, dest).ok
    flipped = dest / "Contents/Artist A/Album 1/01 Opener.mp3"
    data = bytearray(flipped.read_bytes())
    data[100] ^= 0xFF
    flipped.write_bytes(bytes(data))
    (dest / "PIONEER/empty.txt").unlink()
    (dest / "Contents/new.mp3").write_bytes(b"new")
    result = sr.compare_trees(snapshot, dest)
    assert not result.ok
    assert result.mismatched == ["Contents/Artist A/Album 1/01 Opener.mp3"]
    assert result.missing == ["PIONEER/empty.txt"]
    assert result.unexpected == ["Contents/new.mp3"]
    assert result.to_dict()["ok"] is False


def test_cli_receipt_carries_the_byte_compare(
    snapshot: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the CLI restore succeeds [then] its receipt states the byte-compare result, [else stop]."""
    dest = tmp_path / "RESTORE-TARGET"
    assert cli.main(["restore", str(snapshot), str(dest)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ok"] is True
    compare = receipt["byte_compare"]
    assert compare["ok"] is True
    assert compare["method"] == "recursive byte comparison"
    assert compare["files_compared"] == len(SNAP_FILES)
    assert compare["bytes_compared"] == sum(len(b) for b in SNAP_FILES.values())
    assert compare["mismatched"] == compare["missing"] == compare["unexpected"] == []
    manifest_bytes = snap.manifest_path_for(snapshot).read_bytes()
    assert receipt["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()


def test_cli_refusal_exits_one_with_error_json(
    snapshot: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the CLI meets a mismatched snapshot [then] it exits 1 with an error, never a receipt, [else stop]."""
    _same_size_flip(snapshot)
    assert cli.main(["restore", str(snapshot), str(tmp_path / "RESTORE-TARGET")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = json.loads(captured.err)
    assert err["error"] == "manifest_mismatch"
    assert err["entries"] == [{"path": "PIONEER/rekordbox/export.pdb", "problem": "sha256 differs from manifest"}]


def test_python_dash_m_restores_a_fixture_snapshot(snapshot: Path, tmp_path: Path) -> None:
    """[if] ``python -m`` restores a snapshot [then] the bytes match and the receipt says so, [else stop]."""
    dest = tmp_path / "RESTORE-TARGET"
    proc = subprocess.run(
        [sys.executable, "-m", "apps.sync.usb", "restore", str(snapshot), str(dest)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["byte_compare"]["ok"] is True
    assert tree_bytes(dest) == tree_bytes(snapshot)


def test_cli_byte_mismatch_after_restore_exits_one_and_rolls_back(
    snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a restored file differs by one byte at the compare [then] ``restore`` exits 1 and removes its tree, [else stop]."""
    dest = tmp_path / "RESTORE-TARGET"
    victim = "Contents/Artist B/Edit.wav"
    real = sr.compare_trees

    def flip_then_compare(source: Path, restored: Path) -> sr.ByteCompare:  # bit rot on the target stick
        data = bytearray((restored / victim).read_bytes())
        data[5] ^= 0x01
        (restored / victim).write_bytes(bytes(data))
        return real(source, restored)

    monkeypatch.setattr(sr, "compare_trees", flip_then_compare)
    assert cli.main(["restore", str(snapshot), str(dest)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = json.loads(captured.err)
    assert err["error"] == "byte_compare_failed"
    assert err["byte_compare"]["ok"] is False
    assert err["byte_compare"]["mismatched"] == [victim]
    assert not os.path.lexists(dest)


@pytest.mark.parametrize(
    "argv",
    [["restore"], ["restore", "ONLY-SNAPSHOT"], ["restore", "A", "B", "C"], ["restore", "A", "B", "--bogus"]],
)
def test_cli_restore_bad_arguments_exit_two(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """[if] ``restore`` gets the wrong arguments [then] argparse exits 2 and nothing runs, [else stop]."""
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_cli_restore_honours_manifest_flag(snapshot: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] ``--manifest`` names a manifest elsewhere [then] restore uses it and reports its path, [else stop]."""
    moved = tmp_path / "elsewhere.json"
    snap.manifest_path_for(snapshot).rename(moved)
    dest = tmp_path / "RESTORE-TARGET"
    assert cli.main(["restore", str(snapshot), str(dest)]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "manifest_unreadable"
    assert cli.main(["restore", str(snapshot), str(dest), "--manifest", str(moved)]) == 0
    assert json.loads(capsys.readouterr().out)["manifest"] == str(moved)
    assert tree_bytes(dest) == tree_bytes(snapshot)


def test_usage_lists_restore(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the CLI runs with no subcommand [then] its usage names ``restore``, [else stop]."""
    assert cli.main([]) == 0
    assert "restore" in capsys.readouterr().out


def test_restores_a_snapshot_written_by_the_snapshot_command(tmp_path: Path) -> None:
    """[if] ``snapshot`` writes a real #12229 snapshot [then] ``restore`` rebuilds it byte for byte, [else stop]."""
    source = tmp_path / "STICK"
    for rel, data in SNAP_FILES.items():
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_bytes(data)
    for rel in EXTRA_DIRS:
        (source / rel).mkdir(parents=True, exist_ok=True)
    made = snap.create_snapshot(source, tmp_path / "SNAP")
    receipt = sr.restore_snapshot(made.dest, tmp_path / "RESTORE-TARGET")
    assert receipt.ok
    assert receipt.manifest_sha256 == made.manifest_sha256
    assert receipt.files == made.file_count and receipt.dirs == made.dir_count
    assert tree_bytes(tmp_path / "RESTORE-TARGET") == tree_bytes(source)


def test_unicode_form_collision_is_refused(snapshot: Path, tmp_path: Path) -> None:
    """[if] two manifest paths differ only by NFC vs NFD [then] restore refuses up front, [else stop]."""
    nfc, nfd = "PIONEER/caf\u00e9.txt", "PIONEER/cafe\u0301.txt"
    digest = hashlib.sha256(b"").hexdigest()
    edit_manifest(snapshot, lambda d: d["files"].extend({"path": p, "sha256": digest, "size": 0} for p in (nfc, nfd)))
    dest = tmp_path / "RESTORE-TARGET"
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "path_collision"
    assert [e["path"] for e in exc.value.entries] == [nfd]
    assert not os.path.lexists(dest)


pinned_handles = pytest.mark.skipif(not sr._PINNED, reason="needs dir_fd and O_NOFOLLOW (POSIX)")


@pinned_handles
def test_parent_swapped_for_link_never_writes_outside_root(
    snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a restored parent dir is swapped for a link mid-restore [then] nothing lands outside the root, [else stop]."""
    outside = tmp_path / "OUTSIDE"
    outside.mkdir()
    dest = tmp_path / "RESTORE-TARGET"
    real = sr._Tree.create_file
    swapped = {"done": False}

    def swap_then_create(self: sr._Tree, rel: str) -> int:  # an adversary racing the restore
        if not swapped["done"]:
            swapped["done"] = True
            (dest / "Contents").rename(dest / "Contents-moved")
            (dest / "Contents").symlink_to(outside, target_is_directory=True)
        return real(self, rel)

    monkeypatch.setattr(sr._Tree, "create_file", swap_then_create)
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "write_failed"
    assert list(outside.iterdir()) == []
    assert not any(p.is_file() for p in (dest / "Contents-moved").rglob("*"))


@pinned_handles
def test_rollback_leaves_a_replaced_file_untouched(
    snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a restored file is replaced before rollback [then] rollback keeps the replacement and reports it, [else stop]."""
    dest = tmp_path / "RESTORE-TARGET"
    first = "Contents/Artist A/Album 1/01 Opener.mp3"
    real = sr._copy_exclusive
    calls = {"n": 0}

    def copy_then_interfere(source: Path, tree: sr._Tree, sha: str, rel: str) -> int:
        calls["n"] += 1
        if calls["n"] == 2:  # someone moves our first copy and puts their own file there
            (dest / first).rename(dest / "moved-copy.mp3")
            (dest / first).write_bytes(b"not ours")
            raise OSError(28, "No space left on device")
        return real(source, tree, sha, rel)

    monkeypatch.setattr(sr, "_copy_exclusive", copy_then_interfere)
    with pytest.raises(sr.RestoreError) as exc:
        sr.restore_snapshot(snapshot, dest)
    assert exc.value.code == "write_failed"
    assert (dest / first).read_bytes() == b"not ours"
    left = {e["path"] for e in exc.value.extra["cleanup_incomplete"]}
    assert first in left


@pinned_handles
def test_open_handles_stay_bounded_by_depth(snapshot: Path, tmp_path: Path) -> None:
    """[if] a snapshot has more dirs than the fd limit [then] restore still succeeds, [else stop]."""
    resource = pytest.importorskip("resource")
    many = [f"PIONEER/Artwork/d{i:04d}" for i in range(300)]
    for rel in many:
        (snapshot / rel).mkdir()
    edit_manifest(snapshot, lambda d: d.update(dirs=sorted([*d["dirs"], *many])))
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(128, hard), hard))
    try:
        receipt = sr.restore_snapshot(snapshot, tmp_path / "RESTORE-TARGET")
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert receipt.ok
    assert receipt.dirs == len(_all_dirs(SNAP_FILES, EXTRA_DIRS)) + len(many)
