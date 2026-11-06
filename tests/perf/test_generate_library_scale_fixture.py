from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from scripts.perf.generate_library_scale_fixture import build

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_build_writes_exact_track_and_playlist_counts(tmp_path: Path) -> None:
    data_dir = tmp_path / "fixture"

    build(data_dir, tracks=7, seed=2328)

    manifest = json.loads((data_dir / "library-scale-fixture.json").read_text())
    assert manifest["tracks_requested"] == 7
    assert manifest["seed"] == 2328
    assert manifest["track_denominator"] == "present"


def test_manifest_pins_audio_checksum_and_refuses_release_evidence(tmp_path: Path) -> None:
    data_dir = tmp_path / "fixture"

    build(data_dir, tracks=3, seed=2328)

    manifest = json.loads((data_dir / "library-scale-fixture.json").read_text())
    audio = (data_dir / "audio" / "shared-fixture.wav").read_bytes()
    assert manifest["audio_sha256"] == hashlib.sha256(audio).hexdigest()
    assert manifest["release_evidence"] is False
    assert manifest["synthetic"] is True


def _run_cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the real generator CLI in a child process with an explicit cwd."""
    env = {**os.environ, "PYTHONPATH": os.fspath(REPOSITORY_ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "scripts.perf.generate_library_scale_fixture", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_cli_rejects_relative_data_dir_before_writing(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "--data-dir", "relative", "--tracks", "1")

    assert result.returncode == 2, result.stderr
    assert "--data-dir must be absolute" in result.stderr
    assert not (tmp_path / "relative").exists()


def test_cli_accepts_absolute_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "absolute"

    result = _run_cli(tmp_path, "--data-dir", os.fspath(data_dir), "--tracks", "1")

    assert result.returncode == 0, result.stderr
    manifest = json.loads((data_dir / "library-scale-fixture.json").read_text())
    assert manifest["tracks_requested"] == 1


def test_same_seed_is_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    build(first, tracks=5, seed=41)
    build(second, tracks=5, seed=41)

    assert (first / "library-scale-fixture.json").read_bytes() == (second / "library-scale-fixture.json").read_bytes()
    assert (first / "audio" / "shared-fixture.wav").read_bytes() == (
        second / "audio" / "shared-fixture.wav"
    ).read_bytes()


def test_audio_is_deterministic_30_second_non_silent_wav(tmp_path: Path) -> None:
    data_dir = tmp_path / "fixture"

    build(data_dir, tracks=1, seed=2328)

    with wave.open(str(data_dir / "audio" / "shared-fixture.wav"), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 44_100
        assert handle.getnframes() == 30 * 44_100
        assert any(handle.readframes(handle.getnframes()))


# What the bench reads, by value. Item ids, timestamps and origin device ids are
# minted per write by StateWriter, so they differ between any two builds and
# are left out; every table's row count below still covers them.
_DIGEST_QUERIES: tuple[str, ...] = (
    "SELECT stable_id, stable_id_tier, title, artists_json, album, isrc, duration_ms,"
    " file_path, deleted_at FROM tracks ORDER BY stable_id",
    "SELECT stable_id, kind, role, file_path, available, deleted_at FROM track_locations"
    " ORDER BY stable_id, kind, role",
    "SELECT playlist_id, name, vendor, vendor_pl_id, deleted_at FROM playlists ORDER BY playlist_id",
    "SELECT playlist_id, stable_id, position, order_key, deleted_at FROM playlist_memberships"
    " ORDER BY playlist_id, position",
)


def _table_counts(data_dir: Path) -> dict[str, int]:
    connection = sqlite3.connect(data_dir / "state" / "state.db")
    try:
        tables = [
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        ]
        return {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
    finally:
        connection.close()


def _content_digest(data_dir: Path) -> str:
    digest = hashlib.sha256()
    connection = sqlite3.connect(data_dir / "state" / "state.db")
    try:
        for query in _DIGEST_QUERIES:
            digest.update(query.encode("utf-8"))
            for row in connection.execute(query):
                digest.update(json.dumps(row).encode("utf-8"))
    finally:
        connection.close()
    return digest.hexdigest()


def test_consecutive_builds_in_one_directory_yield_the_same_database(tmp_path: Path) -> None:
    """PR #4547 review: a rerun reseeded the kept fixture, so memberships were
    replaced under new item ids and changelog rows and events piled up."""
    data_dir = tmp_path / "fixture"

    build(data_dir, tracks=6, seed=41)
    first_counts = _table_counts(data_dir)
    first_digest = _content_digest(data_dir)
    build(data_dir, tracks=6, seed=41)

    # The tables the defect grew must be populated, or equal counts prove nothing.
    for table in ("playlist_memberships", "local_changelog", "events"):
        assert first_counts[table] > 0, f"{table} is empty after a build"
    assert first_counts["playlist_memberships"] == 6
    assert _table_counts(data_dir) == first_counts
    assert _content_digest(data_dir) == first_digest


def test_rebuild_discards_whatever_a_previous_run_wrote_into_the_fixture(tmp_path: Path) -> None:
    """A bench run's engine writes into the fixture it serves; the next build
    must not carry that forward."""
    data_dir = tmp_path / "fixture"
    build(data_dir, tracks=4, seed=41)
    clean_counts = _table_counts(data_dir)
    clean_digest = _content_digest(data_dir)
    connection = sqlite3.connect(data_dir / "state" / "state.db")
    try:
        connection.execute(
            "UPDATE tracks SET title = 'edited by a previous run' WHERE stable_id = ?",
            ("libm120-track-00000",),
        )
        connection.execute("DELETE FROM playlist_memberships WHERE stable_id = ?", ("libm120-track-00003",))
        connection.commit()
    finally:
        connection.close()
    (data_dir / "left-by-engine.txt").write_text("stale\n", encoding="utf-8")
    assert _content_digest(data_dir) != clean_digest, "the edit did not change the digest"

    build(data_dir, tracks=4, seed=41)

    assert _table_counts(data_dir) == clean_counts
    assert _content_digest(data_dir) == clean_digest
    assert not (data_dir / "left-by-engine.txt").exists()


def test_a_different_seed_or_size_rebuilds_with_different_content(tmp_path: Path) -> None:
    data_dir = tmp_path / "fixture"
    build(data_dir, tracks=5, seed=41)
    baseline = _content_digest(data_dir)

    build(data_dir, tracks=5, seed=42)
    reseeded = _content_digest(data_dir)
    assert reseeded != baseline
    assert json.loads((data_dir / "library-scale-fixture.json").read_text())["seed"] == 42

    build(data_dir, tracks=7, seed=42)
    counts = _table_counts(data_dir)
    assert counts["playlist_memberships"] == 7
    assert counts["tracks"] == 7
    assert _content_digest(data_dir) not in (baseline, reseeded)


def test_refuses_to_empty_a_directory_it_did_not_make(tmp_path: Path) -> None:
    data_dir = tmp_path / "real-data-dir"
    (data_dir / "state").mkdir(parents=True)
    precious = data_dir / "state" / "state.db"
    precious.write_bytes(b"not a fixture")

    with pytest.raises(ValueError, match="refusing to reset"):
        build(data_dir, tracks=2, seed=41)
    assert precious.read_bytes() == b"not a fixture"


def test_an_empty_existing_directory_is_built_in(tmp_path: Path) -> None:
    """The Playwright config creates the directory before the generator runs."""
    data_dir = tmp_path / "fixture"
    data_dir.mkdir()

    build(data_dir, tracks=2, seed=41)

    assert _table_counts(data_dir)["tracks"] == 2
