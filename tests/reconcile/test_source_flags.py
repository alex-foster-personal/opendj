"""SRCFLAG-01..03: flag-only source checks, over a real data dir.

[if] a track's file is not the recording it claims (length vs release, stems source, hard-linked write) [then] the scan flags it and nothing is refused, [else stop].

Real files throughout: a sqlite state.db, a Spotify matches.csv, a stem-set
manifest, WAV audio written with ``wave`` and a real hard link.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.reconcile import source_flags
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

pytestmark = pytest.mark.requirement("SRCFLAG-01")

CARMEN = "68100d1e6030061873b636c60933cd862da18999"
OK = "0209bbb6c2f1dac350c8b811dadda84585fe175c"
STEMS = "00d5cd7513924c89b2bd823b8d268231a0ad8931"


def _wav(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(1)
        out.setframerate(8000)
        out.writeframes(b"\x80" * int(8000 * seconds))
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    music = tmp_path / "music"
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True)
    files = {
        CARMEN: _wav(music / "Opera Dance Music - Carmen.wav", 9.59),  # release 10.0 s: -4.1 %
        OK: _wav(music / "Y do I - Carmina Burana Remix.wav", 10.0),
        STEMS: _wav(music / "Noisia - Nova.wav", 10.0),
    }
    db = sqlite3.connect(state / "state.db")
    db.executescript(
        "CREATE TABLE tracks (stable_id TEXT, title TEXT, artists_json TEXT, duration_ms INTEGER, file_path TEXT, deleted_at TEXT);"
        "CREATE TABLE track_fields (stable_id TEXT, field_name TEXT, value_json TEXT);"
    )
    rows = [
        (CARMEN, "Carmen", '["Opera Dance Music"]', 9590, str(files[CARMEN])),
        (OK, "Carmina Burana Remix", '["Y do I"]', 10000, str(files[OK])),
        (STEMS, "Nova", '["Noisia"]', 10000, str(files[STEMS])),
    ]
    db.executemany("INSERT INTO tracks VALUES (?, ?, ?, ?, ?, NULL)", rows)
    db.execute("INSERT INTO track_fields VALUES (?, 'isrc', ?)", (OK, json.dumps("DEKB72069812")))
    db.commit()
    db.close()
    imports = tmp_path / "data" / "spotify" / "import-viva26-20261103T054229Z"
    imports.mkdir(parents=True)
    with (imports / "matches.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["spotify_uri", "isrc", "title", "artist", "duration_ms", "status"])
        writer.writeheader()
        writer.writerow(
            {
                "spotify_uri": "spotify:track:carmen",
                "isrc": "ITBF82400008",
                "title": "Carmen",
                "artist": "Opera Dance Music",
                "duration_ms": 10000,
                "status": "unmatched",
            }
        )
        writer.writerow(
            {
                "spotify_uri": "spotify:track:ydi",
                "isrc": "DEKB72069812",
                "title": "Something Else",
                "artist": "Y do I",
                "duration_ms": 10010,
                "status": "matched",
            }
        )
    manifest = state / "stems" / STEMS / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"audio": {"sample_rate": 44100, "frame_count": 44100 * 12}}))  # 12 s source
    return tmp_path / "data"


def test_length_mismatch_against_release_flags_carmen_only(data_dir: Path) -> None:
    found = source_flags.scan(data_dir)
    codes = {sid: [f.code for f in flags] for sid, flags in found.items()}
    assert "length_mismatch" in codes.get(CARMEN, []), (
        "if Carmen's 4.1 % short file is not flagged then SRCFLAG-01 misses the incident"
    )
    assert OK not in codes or "length_mismatch" not in codes[OK], (
        "if a 0.1 % ISRC match is flagged then the length flag is noise"
    )
    message = next(f.message for f in found[CARMEN] if f.code == "length_mismatch")
    assert "9.6 s" in message and "10.0 s" in message and "-4.1 %" in message


@pytest.mark.requirement("SRCFLAG-8844")
def test_zero_length_release_flags_nothing_and_does_not_abort_the_scan() -> None:
    """[if] a release with duration 0 raises out of length_flag [then] one row aborts the scan, [else stop]."""
    track = source_flags.LibraryTrack("sid-0", "T", "A", 200.0, None, None)
    assert source_flags.length_flag(track, source_flags.ReleaseLength(0.0, "spotify")) is None


@pytest.mark.requirement("SRCFLAG-02")
def test_stems_from_other_length_recording_flags_by_audio_length_not_hash(data_dir: Path) -> None:
    found = source_flags.scan(data_dir)
    assert [f.code for f in found[STEMS]] == ["stems_other_recording"], (
        "if a 12 s stem source on a 10 s file is not flagged then SRCFLAG-02 is blind"
    )
    # A tag-only change (new sha256, same length) never flags.
    manifest = data_dir / "state" / "stems" / STEMS / "manifest.json"
    manifest.write_text(
        json.dumps({"source": {"sha256": "0" * 64}, "audio": {"sample_rate": 44100, "frame_count": 441000}})
    )
    assert STEMS not in source_flags.scan(data_dir), (
        "if a changed hash alone flags then every Mixed In Key tag write reads as a swap"
    )


@pytest.mark.requirement("SRCFLAG-03")
def test_hardlinked_in_place_write_is_info_and_counts_other_names() -> None:
    flag = source_flags.hardlink_flag(nlink=2, mtime=1_793_827_992.0, birthtime=1_793_052_909.0)
    assert flag is not None and flag.severity == "info", (
        "if a hard-linked in-place write is not flagged then SRCFLAG-03 is blind"
    )
    assert "1 other file name" in flag.message and "Wed 04 Nov 21:33 UTC" in flag.message
    assert source_flags.hardlink_flag(nlink=1, mtime=2e9, birthtime=1e9) is None, (
        "if an unlinked file flags then every edited file is noise"
    )
    assert source_flags.hardlink_flag(nlink=3, mtime=1e9 + 30, birthtime=1e9) is None, (
        "if a write at creation flags then every new hard link is noise"
    )
    assert source_flags.hardlink_flag(nlink=3, mtime=2e9, birthtime=None) is None


@pytest.mark.requirement("SRCFLAG-03")
def test_real_hard_link_reports_its_link_count(tmp_path: Path) -> None:
    first = _wav(tmp_path / "a.wav", 1.0)
    os.link(first, tmp_path / "b.wav")
    assert os.stat(first).st_nlink == 2


@pytest.fixture
def client(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(rb_config, "STATE_DB", data_dir / "state" / "state.db")
    app = create_app(backend=InMemoryBackend(), bind_host="127.0.0.1", hostname="test-host", mount_frontend=False)
    with TestClient(app) as test_client:
        yield test_client


def test_routes_scan_then_list_and_read_one(client: TestClient, data_dir: Path) -> None:
    before = client.get("/api/v1/source-flags")
    assert before.status_code == 404 and before.json()["detail"]["code"] == "SOURCE_FLAGS_NOT_SCANNED", (
        "if an unscanned library reads as clean then a missing scan hides findings"
    )
    scanned = client.post("/api/v1/source-flags/scan")
    assert scanned.status_code == 200, scanned.text
    assert scanned.json()["counts"] == {"length_mismatch": 1, "stems_other_recording": 1}
    listed = client.get("/api/v1/source-flags?code=length_mismatch").json()
    assert [t["stable_id"] for t in listed["tracks"]] == [CARMEN]
    one = client.get(f"/api/v1/source-flags/{CARMEN}").json()
    assert one["flags"][0]["code"] == "length_mismatch"
    assert (data_dir / "state" / "source-flags.json").is_file()
