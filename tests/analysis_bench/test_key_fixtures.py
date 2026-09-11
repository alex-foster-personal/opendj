"""Key-lane fixture builder: tiny sqlite + one wav, then seal and score."""

from __future__ import annotations

import json
import math
import sqlite3
import struct
import wave
from pathlib import Path

import pytest

from apps.analysis_bench import bundles, cli
from apps.analysis_bench.scorers import key_lane
from scripts import build_key_bundle


def _write_tone(path: Path, seconds: float = 3.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 44100
    n = int(seconds * rate)
    with wave.open(str(path), "w") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            value = int(12000 * math.sin(2 * math.pi * 261.63 * i / rate))
            frames += struct.pack("<h", value)
        fh.writeframes(bytes(frames))


def _write_rekordbox(path: Path, *, n_contents: int = 1, folder: str, length_s: int = 3) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE djmdContent (
            ID TEXT PRIMARY KEY,
            FolderPath TEXT,
            FileNameL TEXT,
            Title TEXT,
            BPM INTEGER,
            Length INTEGER,
            Rating INTEGER,
            KeyID TEXT,
            ArtistID TEXT,
            rb_local_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE djmdKey (ID TEXT PRIMARY KEY, ScaleName TEXT);
        CREATE TABLE djmdArtist (ID TEXT PRIMARY KEY, Name TEXT);
        INSERT INTO djmdKey(ID, ScaleName) VALUES ('k1', 'Am');
        INSERT INTO djmdArtist(ID, Name) VALUES ('a1', 'Test Artist');
        """
    )
    for i in range(n_contents):
        folder_path = folder if i == 0 else f"{folder}.{i}"
        conn.execute(
            "INSERT INTO djmdContent(ID, FolderPath, FileNameL, Title, BPM, Length, "
            "Rating, KeyID, ArtistID) VALUES (?, ?, 'tone.wav', 'Tone', 12000, ?, 5, 'k1', 'a1')",
            (f"rb-{i+1}", folder_path, length_s),
        )
    conn.commit()
    conn.close()


def _write_mik(path: Path, file_path: str) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE Song (
            SongId INTEGER PRIMARY KEY,
            File TEXT,
            ArtistName TEXT,
            SongName TEXT,
            MainKey TEXT,
            MainKeyConfidence REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO Song(File, ArtistName, SongName, MainKey, MainKeyConfidence) "
        "VALUES (?, 'Test Artist', 'Tone', '8A', 0.9)",
        (file_path,),
    )
    conn.commit()
    conn.close()


def _tiny_inputs(tmp_path: Path, *, n_contents: int = 1) -> dict[str, Path]:
    orig = "/Users/dev/Music/tone.wav"
    audio_root = tmp_path / "audio"
    wav = audio_root / "abc" / "tone.wav"
    _write_tone(wav, seconds=3.0)
    manifest = tmp_path / "copy-manifest.nfc.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "orig_path": orig,
                "dir": "abc",
                "basename": "tone.wav",
                "rel": "abc/tone.wav",
                "size": wav.stat().st_size,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rb = tmp_path / "master.plain.db"
    _write_rekordbox(rb, n_contents=n_contents, folder=orig, length_s=3)
    mik = tmp_path / "MIKStore.db"
    _write_mik(mik, orig)
    return {
        "rekordbox": rb,
        "mik": mik,
        "audio_root": audio_root,
        "copy_manifest": manifest,
        "orig": Path(orig),
        "wav": wav,
    }


@pytest.mark.requires_ffmpeg
def test_builder_stages_seal_and_controls_round_trip(tmp_path: Path) -> None:
    paths = _tiny_inputs(tmp_path)
    staged = tmp_path / "key" / "v1"
    assert build_key_bundle.main(
        [
            "build",
            "--version", "v1",
            "--rekordbox", str(paths["rekordbox"]),
            "--mik-windows", str(paths["mik"]),
            "--audio-root", str(paths["audio_root"]),
            "--copy-manifest", str(paths["copy_manifest"]),
            "--out", str(staged),
            "--allow-tiny",
            "--seed", "20260910",
            "--sample-size", "200",
        ]
    ) == 0
    assert (staged / "key-truth.json").is_file()
    assert (staged / "wav" / "rb-1.wav").is_file()
    sealed = bundles.seal_bundle(staged, lane="key", version="v1")
    assert sealed["bundle_id"]
    _manifest, rekordbox, mik = key_lane.load_bundle(staged)
    assert list(rekordbox) == ["rb-1"]
    assert rekordbox["rb-1"] == canon_am()
    assert mik["rb-1"] == canon_am()

    report_path = tmp_path / "report.json"
    assert cli.main(
        [
            "run", "--lane", "key", "--candidate", "constant_key",
            "--bundle-dir", str(staged), "--version", "v1",
            "--workdir", str(tmp_path / "arms"),
            "--out", str(report_path),
        ]
    ) == 0
    report = json.loads(report_path.read_text())
    assert report["scorer_version"] == "1.0.0"
    ceiling = report["arms"]["truth_echo"]["vs_rekordbox"]["mirex_mean_pct"]
    floor = report["arms"]["constant_key"]["vs_rekordbox"]["mirex_mean_pct"]
    assert ceiling - floor > 5.0
    assert report["arms"]["truth_echo_mik"]["vs_mik"]["mirex_mean_pct"] == 100.0


@pytest.mark.requires_ffmpeg
def test_fixtures_build_via_cli_allow_tiny(tmp_path: Path) -> None:
    paths = _tiny_inputs(tmp_path)
    staged = tmp_path / "staged"
    assert cli.main(
        [
            "fixtures", "build", "--lane", "key", "--version", "v1",
            "--dir", str(staged),
            "--rekordbox", str(paths["rekordbox"]),
            "--mik-windows", str(paths["mik"]),
            "--audio-root", str(paths["audio_root"]),
            "--copy-manifest", str(paths["copy_manifest"]),
            "--allow-tiny",
        ]
    ) == 0
    assert (staged / "key-truth.json").is_file()


def test_builder_refuses_tiny_rekordbox_without_allow_tiny(tmp_path: Path) -> None:
    paths = _tiny_inputs(tmp_path, n_contents=1)
    with pytest.raises(SystemExit, match="djmdContent"):
        build_key_bundle.main(
            [
                "build",
                "--rekordbox", str(paths["rekordbox"]),
                "--mik-windows", str(paths["mik"]),
                "--audio-root", str(paths["audio_root"]),
                "--copy-manifest", str(paths["copy_manifest"]),
                "--out", str(tmp_path / "out"),
            ]
        )


def test_builder_refuses_zero_resolvable_audio_even_when_large(tmp_path: Path) -> None:
    paths = _tiny_inputs(tmp_path, n_contents=100)
    # Point audio-root at an empty dir so nothing resolves.
    empty_audio = tmp_path / "empty-audio"
    empty_audio.mkdir()
    with pytest.raises(SystemExit, match="0 FolderPath"):
        build_key_bundle.main(
            [
                "build",
                "--rekordbox", str(paths["rekordbox"]),
                "--mik-windows", str(paths["mik"]),
                "--audio-root", str(empty_audio),
                "--copy-manifest", str(paths["copy_manifest"]),
                "--out", str(tmp_path / "out"),
            ]
        )


def test_builder_refuses_zero_byte_rekordbox(tmp_path: Path) -> None:
    empty = tmp_path / "master.plain.db"
    empty.write_bytes(b"")
    with pytest.raises(SystemExit, match="0 bytes"):
        build_key_bundle.main(
            [
                "build",
                "--rekordbox", str(empty),
                "--mik-windows", str(tmp_path / "mik.db"),
                "--audio-root", str(tmp_path / "audio"),
                "--copy-manifest", str(tmp_path / "m.jsonl"),
                "--out", str(tmp_path / "out"),
                "--allow-tiny",
            ]
        )


def test_windows_mik_file_joins_via_audio_rel(tmp_path: Path) -> None:
    """Basename is not unique in the real corpus; the hashed dir in File is."""
    from scripts.build_key_bundle import (
        CopyRow,
        Denominators,
        _index_copy_manifest,
        _match_mik_to_copy,
        _rel_from_windows_mik_file,
    )

    assert (
        _rel_from_windows_mik_file(
            r"C:\mik-run-20260908\audio\abc123\track.mp3"
        )
        == "abc123/track.mp3"
    )
    rows = [
        CopyRow("/Users/dev/Music/track.mp3", "abc123/track.mp3", "track.mp3", 1),
        CopyRow("/Users/dev/Other/track.mp3", "def456/track.mp3", "track.mp3", 1),
    ]
    by_orig, by_base, by_rel = _index_copy_manifest(rows)
    den = Denominators()
    winners = _match_mik_to_copy(
        [
            {
                "row_id": "guid-1",
                "file_path": r"C:\mik-run-20260908\audio\abc123\track.mp3",
                "main_key": "8A",
                "confidence": 0.9,
            }
        ],
        by_orig,
        by_base,
        by_rel,
        den,
    )
    assert list(winners) == ["abc123/track.mp3"]
    assert winners["abc123/track.mp3"]["tier"] == "windows_rel"


def canon_am():
    from apps.analysis_key.canon import from_rekordbox_scale_name

    return from_rekordbox_scale_name("Am")
