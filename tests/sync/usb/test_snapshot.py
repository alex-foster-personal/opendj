"""Tests for the USB stick snapshot command (issue #12229, USBEXP-12729).

Every test builds its stick tree under ``tmp_path`` (synthesized here, no
fixture files) and runs the real snapshot and verify code with no doubles.

- [if] a stick tree with nested audio and metadata files is snapshotted [then] each regular file is hard-linked, never copied, [else stop].
- [if] a snapshot completes [then] its manifest has a relative path, size and SHA-256 for every source file, [else stop].
- [if] a snapshot file changes after the manifest is written [then] verification reports it as a mismatch, never as matching, [else stop].
- [if] the source holds a symlink or unsupported entry [then] it is refused or recorded as unsupported, never as a verified regular file, [else stop].
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from apps.sync.usb import snapshot as snap
from apps.sync.usb.__main__ import main

pytestmark = pytest.mark.requirement("USBEXP-12729")

REPO_ROOT = Path(__file__).resolve().parents[3]

# Synthesized stick tree: nested audio, analysis and metadata files.
STICK_FILES: dict[str, bytes] = {
    "Contents/Artist A/Album 1/01 Track.mp3": b"ID3\x03" + b"\x00" * 64 + b"track-one",
    "Contents/Artist A/Album 1/02 Track.flac": b"fLaC" + b"\x01" * 80,
    "Contents/Artist B/Single.wav": b"RIFF" + b"\x02" * 48 + b"WAVE",
    "PIONEER/rekordbox/export.pdb": b"\x00\x00\x00\x00pdb-bytes",
    "PIONEER/USBANLZ/P016/0000A1B2/ANLZ0000.DAT": b"PMAI" + b"\x03" * 32,
    "PIONEER/USBANLZ/P016/0000A1B2/ANLZ0000.EXT": b"PMAI" + b"\x04" * 32,
    "PIONEER/DEVSETTING.DAT": b"devsetting",
    ".fseventsd/no_log": b"",
}


def _make_stick(root: Path) -> Path:
    for rel, data in STICK_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "PIONEER" / "Artwork").mkdir(parents=True)  # empty dir kept as a dir
    return root


@pytest.fixture
def stick(tmp_path: Path) -> Path:
    return _make_stick(tmp_path / "STICK")


def _manifest(dest: Path) -> dict:
    return json.loads(snap.manifest_path_for(dest).read_text(encoding="utf-8"))


def test_snapshot_hard_links_every_regular_file(stick: Path, tmp_path: Path) -> None:
    """[if] nested audio and metadata files are snapshotted [then] each is a hard link to its source, [else stop]."""
    dest = tmp_path / "snap"
    result = snap.create_snapshot(stick, dest)

    assert result.file_count == len(STICK_FILES)
    for rel in STICK_FILES:
        src_stat = (stick / rel).stat()
        dst_stat = (dest / rel).stat()
        assert (dst_stat.st_dev, dst_stat.st_ino) == (src_stat.st_dev, src_stat.st_ino), rel
        assert dst_stat.st_nlink >= 2, rel
        assert not (dest / rel).is_symlink()
    assert (dest / "PIONEER" / "Artwork").is_dir()


def test_snapshot_never_copies_when_hard_link_fails(
    stick: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the filesystem refuses a hard link [then] the snapshot fails and copies nothing, [else stop]."""

    def refuse_link(src: object, dst: object, **_: object) -> None:
        raise OSError(18, "Invalid cross-device link")  # EXDEV

    # The double stands in for the OS's link call (a dependency), not the snapshot.
    monkeypatch.setattr(snap.os, "link", refuse_link)
    dest = tmp_path / "snap"
    with pytest.raises(snap.SnapshotError) as exc:
        snap.create_snapshot(stick, dest)
    assert exc.value.code == "hard_link_failed"
    assert not dest.exists()
    assert not snap.manifest_path_for(dest).exists()
    assert [p.name for p in tmp_path.iterdir()] == ["STICK"]


def test_manifest_lists_path_size_and_sha256_for_every_source_file(stick: Path, tmp_path: Path) -> None:
    """[if] a snapshot completes [then] the manifest names every source file with its SHA-256, [else stop]."""
    dest = tmp_path / "snap"
    result = snap.create_snapshot(stick, dest)
    manifest = _manifest(dest)

    assert result.manifest_path == snap.manifest_path_for(dest)
    assert snap.manifest_path_for(dest).parent == dest.parent  # beside, not inside
    assert manifest["schema"] == snap.MANIFEST_SCHEMA
    assert manifest["unsupported"] == []
    by_path = {entry["path"]: entry for entry in manifest["files"]}
    assert set(by_path) == set(STICK_FILES)
    for rel, data in STICK_FILES.items():
        assert by_path[rel]["sha256"] == hashlib.sha256(data).hexdigest()
        assert by_path[rel]["size"] == len(data)
    assert [e["path"] for e in manifest["files"]] == sorted(STICK_FILES)
    assert "PIONEER/Artwork" in manifest["dirs"]


def test_manifest_is_deterministic(stick: Path, tmp_path: Path) -> None:
    """[if] the same tree is snapshotted twice [then] both manifests are byte for byte equal, [else stop]."""
    first = snap.create_snapshot(stick, tmp_path / "a")
    second = snap.create_snapshot(stick, tmp_path / "b")
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.manifest_sha256 == hashlib.sha256(first.manifest_path.read_bytes()).hexdigest()


def test_verify_matches_an_untouched_snapshot(stick: Path, tmp_path: Path) -> None:
    """[if] nothing changed since the snapshot [then] verify reports ok, [else stop]."""
    dest = tmp_path / "snap"
    snap.create_snapshot(stick, dest)
    report = snap.verify_snapshot(dest)
    assert report.ok
    assert report.mismatched == [] and report.missing == [] and report.unexpected == []
    assert len(report.matched) == len(STICK_FILES)


def test_verify_reports_a_changed_file_as_mismatch(stick: Path, tmp_path: Path) -> None:
    """[if] a snapshot file changes after manifest creation [then] verify never reports it as matching, [else stop]."""
    dest = tmp_path / "snap"
    snap.create_snapshot(stick, dest)
    target = dest / "PIONEER/rekordbox/export.pdb"
    target.write_bytes(target.read_bytes()[:-1] + b"X")  # same size, new bytes

    report = snap.verify_snapshot(dest)
    assert not report.ok
    assert report.mismatched == ["PIONEER/rekordbox/export.pdb"]
    assert "PIONEER/rekordbox/export.pdb" not in report.matched


def test_verify_reports_missing_replaced_and_extra_files(stick: Path, tmp_path: Path) -> None:
    """[if] a snapshot file is removed, swapped for a symlink or joined by a new one [then] verify fails, [else stop]."""
    dest = tmp_path / "snap"
    snap.create_snapshot(stick, dest)
    (dest / "Contents/Artist B/Single.wav").unlink()
    swapped = dest / "PIONEER/DEVSETTING.DAT"
    swapped.unlink()
    swapped.symlink_to(stick / "PIONEER/DEVSETTING.DAT")
    (dest / "Contents/new.mp3").write_bytes(b"new")

    report = snap.verify_snapshot(dest)
    assert not report.ok
    assert report.missing == ["Contents/Artist B/Single.wav"]
    assert report.not_regular == ["PIONEER/DEVSETTING.DAT"]
    assert report.unexpected == ["Contents/new.mp3"]
    assert "PIONEER/DEVSETTING.DAT" not in report.matched


def test_verify_detects_a_tampered_manifest_digest(stick: Path, tmp_path: Path) -> None:
    """[if] the manifest's recorded digest is edited [then] verify reports that file as a mismatch, [else stop]."""
    dest = tmp_path / "snap"
    snap.create_snapshot(stick, dest)
    path = snap.manifest_path_for(dest)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    report = snap.verify_snapshot(dest)
    assert not report.ok
    assert report.mismatched == [manifest["files"][0]["path"]]


def test_symlink_in_source_is_refused_by_default(stick: Path, tmp_path: Path) -> None:
    """[if] the source holds a symlink [then] the snapshot is refused naming it, nothing is written, [else stop]."""
    (stick / "Contents/link.mp3").symlink_to(stick / "Contents/Artist B/Single.wav")
    (stick / "Contents/linkdir").symlink_to(stick / "PIONEER")
    dest = tmp_path / "snap"
    with pytest.raises(snap.SnapshotError) as exc:
        snap.create_snapshot(stick, dest)
    assert exc.value.code == "unsupported_entries"
    assert {e["path"] for e in exc.value.details} == {"Contents/link.mp3", "Contents/linkdir"}
    assert {e["kind"] for e in exc.value.details} == {"symlink"}
    assert not dest.exists()
    assert not snap.manifest_path_for(dest).exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX only")
def test_unsupported_entries_are_recorded_never_verified_when_allowed(stick: Path, tmp_path: Path) -> None:
    """[if] unsupported entries are allowed [then] the manifest records them as unsupported, never as verified files, [else stop]."""
    (stick / "Contents/link.mp3").symlink_to(stick / "Contents/Artist B/Single.wav")
    os.mkfifo(stick / "PIONEER/pipe")
    dest = tmp_path / "snap"
    snap.create_snapshot(stick, dest, allow_unsupported=True)

    manifest = _manifest(dest)
    file_paths = {e["path"] for e in manifest["files"]}
    assert file_paths == set(STICK_FILES)
    assert manifest["unsupported"] == [
        {"kind": "symlink", "path": "Contents/link.mp3"},
        {"kind": "fifo", "path": "PIONEER/pipe"},
    ]
    for rel in ("Contents/link.mp3", "PIONEER/pipe"):
        assert not os.path.lexists(dest / rel)
    report = snap.verify_snapshot(dest)
    assert report.ok
    assert report.unsupported == ["Contents/link.mp3", "PIONEER/pipe"]
    assert not set(report.unsupported) & set(report.matched)


@pytest.mark.parametrize(
    ("mode", "kind"),
    [
        (stat.S_IFSOCK, "socket"),
        (stat.S_IFCHR, "device"),
        (stat.S_IFBLK, "device"),
        (stat.S_IFLNK, "symlink"),
        (stat.S_IFREG, "file"),
    ],
)
def test_entry_kinds_never_read_special_files_as_regular(mode: int, kind: str) -> None:
    """[if] an entry is a socket, device or symlink [then] its kind is never file, [else stop]."""
    assert snap._kind(mode | 0o644) == kind


def test_snapshot_refuses_unsafe_source_and_destination(stick: Path, tmp_path: Path) -> None:
    """[if] the source is not a real directory or the destination exists or sits inside it [then] refuse, [else stop]."""
    with pytest.raises(snap.SnapshotError) as exc:
        snap.create_snapshot(tmp_path / "nope", tmp_path / "s1")
    assert exc.value.code == "source_not_directory"
    link = tmp_path / "stick-link"
    link.symlink_to(stick)
    with pytest.raises(snap.SnapshotError) as exc:
        snap.create_snapshot(link, tmp_path / "s2")
    assert exc.value.code == "source_is_symlink"
    (tmp_path / "taken").mkdir()
    with pytest.raises(snap.SnapshotError) as exc:
        snap.create_snapshot(stick, tmp_path / "taken")
    assert exc.value.code == "destination_exists"
    with pytest.raises(snap.SnapshotError) as exc:
        snap.create_snapshot(stick, stick / "inner")
    assert exc.value.code == "destination_inside_source"
    assert not (stick / "inner").exists()


def test_cli_snapshot_then_verify_detects_tampering(
    stick: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the CLI snapshots then a file is changed [then] snapshot-verify exits 1 naming it, [else stop]."""
    dest = tmp_path / "snap"
    assert main(["snapshot", str(stick), str(dest)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["files"] == len(STICK_FILES)
    assert out["manifest"] == str(snap.manifest_path_for(dest))
    assert out["manifest_sha256"] == hashlib.sha256(snap.manifest_path_for(dest).read_bytes()).hexdigest()

    assert main(["snapshot-verify", str(dest)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    (dest / "Contents/Artist A/Album 1/01 Track.mp3").write_bytes(b"tampered")
    assert main(["snapshot-verify", str(dest)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["mismatched"] == ["Contents/Artist A/Album 1/01 Track.mp3"]


def test_cli_refusal_exits_one_with_error_json(stick: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the CLI meets a symlink in the source [then] it exits 1 with an error, never success, [else stop]."""
    (stick / "loop").symlink_to(stick)
    assert main(["snapshot", str(stick), str(tmp_path / "snap")]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == "unsupported_entries"
    assert err["entries"] == [{"kind": "symlink", "path": "loop"}]


def test_cli_usage_and_unknown_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the CLI gets no or an unknown subcommand [then] it prints usage or exits 2, [else stop]."""
    assert main([]) == 0
    assert "snapshot" in capsys.readouterr().out
    assert main(["bogus"]) == 2


def test_python_dash_m_entry_point(stick: Path, tmp_path: Path) -> None:
    """[if] ``python -m apps.sync.usb snapshot`` runs [then] it writes a hard-linked snapshot and manifest, [else stop]."""
    dest = tmp_path / "snap"
    proc = subprocess.run(
        [sys.executable, "-m", "apps.sync.usb", "snapshot", str(stick), str(dest)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert snap.manifest_path_for(dest).is_file()
    rel = "PIONEER/rekordbox/export.pdb"
    assert (dest / rel).stat().st_ino == (stick / rel).stat().st_ino


def test_manifest_write_failure_leaves_no_partial_snapshot(
    stick: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "snap"
    real_replace = os.replace

    def boom(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(snap.os, "replace", boom)
    with pytest.raises(snap.SnapshotError) as err:
        snap.create_snapshot(stick, dest)
    monkeypatch.setattr(snap.os, "replace", real_replace)
    assert err.value.code == "write_failed"
    assert not dest.exists()
    assert not snap.manifest_path_for(dest).exists()
    assert list(tmp_path.glob(".snap*")) == []
    snap.create_snapshot(stick, dest)  # a retry is not blocked


def test_unreadable_file_is_a_snapshot_error_not_a_traceback(
    stick: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "snap"
    snap.create_snapshot(stick, dest)
    real_open = open

    def deny(path: object, *args: object, **kwargs: object):
        if str(path).endswith("export.pdb"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", deny)
    with pytest.raises(snap.SnapshotError) as err:
        snap.verify_snapshot(dest)
    assert err.value.code == "read_failed"


def test_scan_of_unreadable_directory_is_a_snapshot_error(stick: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(path: object):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(snap.os, "scandir", deny)
    with pytest.raises(snap.SnapshotError) as err:
        snap.create_snapshot(stick, stick.parent / "snap")
    assert err.value.code == "scan_failed"
