"""Classic rekordbox device export to a directory (USBEXP-10743, issue #10040).

[if] classic plan apply and readback run in a temp dir [then] the stick tree round-trips, [else stop].
"""

from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from apps.sync.usb.pioneer.__main__ import main
from apps.sync.usb.pioneer.anlz_track import read_stick_track_analysis
from apps.sync.usb.pioneer.classic_export import (
    ClassicExportError,
    ClassicPlaylistInput,
    ClassicTrackInput,
    apply_classic_export,
    plan_classic_export,
    readback_classic_export,
)
from apps.sync.usb.pioneer.reader import read_usb_export, validate_invariants
from apps.sync.usb.pioneer.anlz_writer import analyze_paths

pytestmark = pytest.mark.requirement("USBEXP-10743")


def _tiny_wav(path: Path, *, nframes: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x01" * nframes)


@pytest.fixture
def three_tracks(tmp_path: Path) -> list[ClassicTrackInput]:
    specs = [
        ("Alpha", "Artist One", "Album A", 128.0, "8A"),
        ("Beta", "Artist Two", "Album B", 130.0, "5m"),
        ("Gamma", "Artist One", "Album A", 124.0, "12m"),
    ]
    out: list[ClassicTrackInput] = []
    for index, (title, artist, album, bpm, key) in enumerate(specs, start=1):
        src = tmp_path / f"{title.lower()}.wav"
        _tiny_wav(src)
        out.append(
            ClassicTrackInput(
                source_path=src,
                title=title,
                artist=artist,
                album=album,
                bpm=bpm,
                key_name=key,
            )
        )
    return out


@pytest.fixture
def playlists() -> list[ClassicPlaylistInput]:
    return [
        ClassicPlaylistInput(name="Set A", pdb_track_ids=(1, 2)),
        ClassicPlaylistInput(name="Set B", pdb_track_ids=(2, 3)),
    ]


def test_plan_lists_destinations_sources_and_plan_id(three_tracks, playlists) -> None:
    """[if] 3 tracks in 2 playlists are planned [then] every dest and source sha256 is listed with a plan id, [else stop]."""
    plan = plan_classic_export(three_tracks, playlists)
    assert plan.plan_id, "if plan_id is missing then planning is broken"
    hashed = [f for f in plan.files if f.sha256]
    assert len(hashed) == 3, "if not every source has a hash entry then planning is broken"
    dests = {f.destination for f in plan.files}
    assert "/PIONEER/rekordbox/export.pdb" in dests, "if export.pdb is not planned then broken"
    assert all(f.size_bytes > 0 for f in hashed), "if a source size is not recorded then broken"


def test_apply_refuses_nonempty_target_and_changed_source(three_tracks, playlists, tmp_path: Path) -> None:
    """[if] apply targets a nonempty dir or changed sources [then] it refuses, [else stop]."""
    plan = plan_classic_export(three_tracks, playlists)
    target = tmp_path / "stick"
    target.mkdir()
    (target / "leftover.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ClassicExportError, match="not empty"):
        apply_classic_export(plan, target)
    target_empty = tmp_path / "empty"
    apply_classic_export(plan, target_empty)
    mutated = Path(three_tracks[0].source_path)
    mutated.write_bytes(mutated.read_bytes() + b"x")
    target2 = tmp_path / "empty2"
    with pytest.raises(ClassicExportError) as exc_info:
        apply_classic_export(plan, target2)
    assert exc_info.value.code == "source_changed", "if changed sources are not refused then broken"


def test_after_apply_read_usb_export_and_invariants(three_tracks, playlists, tmp_path: Path) -> None:
    """[if] apply completes [then] read_usb_export lists 3 tracks and validate_invariants is clean, [else stop]."""
    plan = plan_classic_export(three_tracks, playlists)
    target = tmp_path / "export"
    apply_classic_export(plan, target)
    data = read_usb_export(target)
    assert len(data["tracks"]) == 3, "if track count is not 3 then export is broken"
    order_titles = [t["title"] for t in data["tracks"]]
    assert order_titles == ["Alpha", "Beta", "Gamma"], "if titles are wrong then pdb order is broken"
    assert [t["bpm"] for t in data["tracks"]] == [128.0, 130.0, 124.0], "if bpm is wrong then broken"
    assert [t["key"] for t in data["tracks"]] == ["8A", "5m", "12m"], "if key is wrong then broken"
    playlists_by_name = {p["name"]: p["track_ids"] for p in data["playlists"] if not p.get("is_folder")}
    assert playlists_by_name["Set A"] == [1, 2], "if Set A order is wrong then broken"
    assert playlists_by_name["Set B"] == [2, 3], "if Set B order is wrong then broken"
    errors = validate_invariants(data)
    assert not errors, f"if invariants fail then export is broken: {errors}"


def test_pioneer_export_verify_exits_zero(three_tracks, playlists, tmp_path: Path) -> None:
    """[if] classic export lands [then] verify --pioneer-export exits 0, [else stop]."""
    plan = plan_classic_export(three_tracks, playlists)
    target = tmp_path / "export"
    apply_classic_export(plan, target)
    proc = subprocess.run(
        [sys.executable, "-m", "apps.sync.usb.verify", "--pioneer-export", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"if verify fails then export tree is wrong: {proc.stderr}"


def test_each_track_has_own_anlz_with_grid_and_cues(three_tracks, playlists, tmp_path: Path) -> None:
    """[if] each track has analyze_path [then] grid and cues round-trip and dirs are unique, [else stop]."""
    plan = plan_classic_export(three_tracks, playlists)
    target = tmp_path / "export"
    apply_classic_export(plan, target)
    data = read_usb_export(target)
    anlz_dirs: set[str] = set()
    for track in data["tracks"]:
        ap = track["analyze_path"]
        assert ap and ap.endswith("ANLZ0000.DAT"), "if analyze_path is wrong then broken"
        anlz_dirs.add(str(Path(ap).parent))
        assert (target / ap.lstrip("/")).is_file(), "if DAT is missing then broken"
        analysis = read_stick_track_analysis(volume_root=target, analyze_path=ap, points=256)
        assert analysis.payload["beatgrid"]["beat_count"] >= 1, "if grid is empty then broken"
        assert analysis.hot_cues, "if cues are missing then broken"
    assert len(anlz_dirs) == 3, "if two tracks share an ANLZ directory then broken"


def test_audio_copied_under_contents_with_sanitised_segments(three_tracks, playlists, tmp_path: Path) -> None:
    """[if] apply copies audio [then] paths sit under Contents with sanitised segments, [else stop]."""
    dirty = ClassicTrackInput(
        source_path=three_tracks[0].source_path,
        title="Bad:Name?",
        artist="Star*One",
        album="Disk|Two",
        bpm=128.0,
        key_name="8A",
    )
    plan = plan_classic_export([dirty], [ClassicPlaylistInput(name="Only", pdb_track_ids=(1,))])
    target = tmp_path / "export"
    apply_classic_export(plan, target)
    data = read_usb_export(target)
    track = data["tracks"][0]
    assert track["path"].startswith("/Contents/Star_One/Disk_Two/"), "if sanitise failed then broken"
    on_disk = target / track["path"].lstrip("/")
    assert on_disk.is_file(), "if audio was not copied then broken"
    assert track["filename"] in track["path"], "if filename does not point at copy then broken"


def test_readback_verified_and_reports_first_mismatch(three_tracks, playlists, tmp_path: Path) -> None:
    """[if] readback runs after apply [then] verified true until a file is tampered, [else stop]."""
    plan = plan_classic_export(three_tracks, playlists)
    target = tmp_path / "export"
    apply_classic_export(plan, target)
    good = readback_classic_export(plan, target)
    assert good["verified"] is True, "if readback is not verified then broken"
    first_audio = next(f for f in plan.files if f.sha256)
    tampered = target / first_audio.destination.lstrip("/")
    data = tampered.read_bytes()
    tampered.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))
    bad = readback_classic_export(plan, target)
    assert bad["verified"] is False and bad["mismatch"]["reason"] == "hash", "if mismatch is not reported then broken"


def test_classic_apply_skips_writeback_gate(three_tracks, playlists, tmp_path: Path, monkeypatch) -> None:
    """[if] classic apply runs [then] require_writeback_enabled is not consulted, [else stop]."""

    def _boom(_label: str) -> None:
        raise AssertionError("writeback gate should not run for classic export")

    monkeypatch.setattr("apps.shared.rekordbox_writeback.require_writeback_enabled", _boom)
    plan = plan_classic_export(three_tracks, playlists)
    apply_classic_export(plan, tmp_path / "stick")


def test_cli_classic_plan_apply_readback(three_tracks, playlists, tmp_path: Path) -> None:
    """[if] classic CLI plan apply readback [then] all exit 0 without the UI, [else stop]."""
    tracks_json = json.dumps(
        [
            {
                "source": str(t.source_path),
                "title": t.title,
                "artist": t.artist,
                "album": t.album,
                "bpm": t.bpm,
                "key": t.key_name,
            }
            for t in three_tracks
        ]
    )
    playlists_json = json.dumps([{"name": p.name, "track_ids": list(p.pdb_track_ids)} for p in playlists])
    assert main(["classic", "plan", "--tracks", tracks_json, "--playlists", playlists_json]) == 0
    plan_path = tmp_path / "plan.json"
    plan = plan_classic_export(three_tracks, playlists)
    plan_path.write_text(json.dumps(plan.to_payload()), encoding="utf-8")
    target = tmp_path / "out"
    assert main(["classic", "apply", "--target", str(target), "--plan", str(plan_path)]) == 0
    assert main(["classic", "readback", "--target", str(target), "--plan", str(plan_path)]) == 0


def test_usbexp_requirement_registered_in_reqs_json() -> None:
    """[if] USBEXP-10743 ships [then] reqs.json documents classic export, [else stop]."""
    payload = json.loads(Path("reqs.json").read_text(encoding="utf-8"))
    ids = {entry["id"] for entry in payload["v1"]["USBEXP"]["requirements"]}
    assert "USBEXP-10743" in ids, "if reqs.json lacks USBEXP-10743 then broken"


def test_analyze_paths_never_collide_for_distinct_sources(three_tracks) -> None:
    """[if] two tracks are planned [then] analyze_paths never share a directory, [else stop]."""
    from apps.shared.hashing import sha256_file_hex

    a = sha256_file_hex(three_tracks[0].source_path)
    b = sha256_file_hex(three_tracks[1].source_path)
    _, ap_a = analyze_paths(pdb_track_id=1, content_sha256_hex=a)
    _, ap_b = analyze_paths(pdb_track_id=2, content_sha256_hex=b)
    assert Path(ap_a).parent != Path(ap_b).parent, "if ANLZ dirs collide then broken"
