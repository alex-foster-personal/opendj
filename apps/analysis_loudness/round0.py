"""Run the native loudness round-0 reference gate on named audio fixtures.

Usage:
  uv run --no-sync --with pyloudnorm --with scipy python -m apps.analysis_loudness.round0 \
    --manifest MANIFEST.json --mik-db COPY.db --audio-root FIXTURES_ROOT FILE [FILE ...]

``--no-sync`` stops a bare ``uv run`` from syncing this worktree's own venv
before applying the ``--with`` overlays, which can prune opt-in packages
already installed there (tests/test_uv_run_no_sync.py documents the outage;
Codex P2 on PR #1491).

``--manifest`` is a required JSON object of ``{path: sha256}`` pinning every
input file's bytes (see :func:`_verify_fixture_bytes`); the resolved
``pyloudnorm``/``scipy`` versions are recorded in the run's own metadata
(``reference_dependencies``) rather than pinned in this docstring, so a round
always states the reference implementation it actually ran against
(Codex P2 on PR #1491).

The MIK ``Song.OverallVolume`` value is emitted as a reported-only control.
It is not a truth source and it cannot affect this command's exit status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import numpy as np
import pyloudnorm  # type: ignore[import-not-found]
from scipy import signal

from apps.analysis_loudness import analyze_file

LUFS_TOLERANCE_LU = 0.1
TRUE_PEAK_TOLERANCE_DB = 0.1
SCORER_VERSION = "1.0.0"

#: The reference implementations' resolved versions, read from the actually
#: installed overlay rather than typed by hand, so a round can never claim a
#: version it did not run (Codex P2 on PR #1491: an unversioned ``--with``
#: overlay can silently run a different reference release across rounds).
REFERENCE_DEPENDENCIES: dict[str, str] = {
    "pyloudnorm": _pkg_version("pyloudnorm"),
    "scipy": _pkg_version("scipy"),
}

#: SQL LIKE's own escape character, doubled below for a literal backslash.
_LIKE_ESCAPE = "\\"


def _escape_like(value: str) -> str:
    """Escape LIKE metacharacters (and the escape character itself)."""
    return (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("_", f"{_LIKE_ESCAPE}_")
        .replace("%", f"{_LIKE_ESCAPE}%")
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_fixture_bytes(path: Path, manifest: dict[str, str]) -> str:
    """Fail fast unless ``path``'s sha256 matches its pinned manifest entry.

    A fixture relinked, re-encoded or replaced at the same path is otherwise
    indistinguishable from the one a round actually measured (Codex P1 on PR
    #1491): the manifest is what makes a round's output verifiable acceptance
    evidence rather than a measurement of whatever happens to be at that path
    today.
    """
    key = str(path)
    digest = _sha256_file(path)
    expected = manifest.get(key)
    if expected is None:
        raise RuntimeError(
            f"{key} has no entry in --manifest; an unpinned fixture cannot be "
            "treated as verified acceptance evidence"
        )
    if digest != expected:
        raise RuntimeError(
            f"{key} sha256 is {digest} but the manifest pins {expected}; the "
            "fixture bytes changed since the manifest was recorded"
        )
    return digest


def _decode(path: Path) -> tuple[np.ndarray, int]:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip().isdigit():
        raise RuntimeError(f"ffprobe could not read sample rate for {path}: {probe.stderr}")
    sample_rate = int(probe.stdout.strip())
    decoded = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
            "-f", "f32le", "-ac", "2", "-ar", str(sample_rate), "-",
        ],
        capture_output=True,
        check=False,
    )
    if decoded.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode {path}: {decoded.stderr.decode()}")
    samples = np.frombuffer(decoded.stdout, dtype="<f4")
    if samples.size == 0 or samples.size % 2:
        raise RuntimeError(f"ffmpeg decoded no complete stereo frames for {path}")
    return samples.reshape(-1, 2), sample_rate


def _db(value: float) -> float:
    return float("-inf") if value <= 0 else float(20.0 * np.log10(value))


def _reference_true_peak_dbtp(samples: np.ndarray) -> float:
    return _db(float(np.abs(signal.resample_poly(samples, 4, 1, axis=0)).max()))


def _mik_volume(connection: sqlite3.Connection, relative_path: Path) -> float:
    suffix = "\\" + str(relative_path).replace("/", "\\")
    pattern = "%" + _escape_like(suffix)
    rows = connection.execute(
        "SELECT OverallVolume FROM Song WHERE File LIKE ? ESCAPE '\\'", (pattern,)
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"MIK Song.OverallVolume match for {relative_path} was {len(rows)}, expected 1"
        )
    return float(rows[0][0])


def _score_file(
    path: Path,
    audio_root: Path | None,
    connection: sqlite3.Connection | None,
    manifest: dict[str, str],
) -> dict[str, Any]:
    digest = _verify_fixture_bytes(path, manifest)
    measured = analyze_file(path)
    samples, sample_rate = _decode(path)
    reference_lufs = float(pyloudnorm.Meter(sample_rate).integrated_loudness(samples))
    reference_dbtp = _reference_true_peak_dbtp(samples)
    row: dict[str, Any] = {
        "path": str(path),
        "sha256": digest,
        "lufs": measured.integrated_lufs,
        "lufs_reference_pyloudnorm": reference_lufs,
        "lufs_delta_lu": measured.integrated_lufs - reference_lufs,
        "dbtp": measured.true_peak_dbtp,
        "dbtp_reference_4x": reference_dbtp,
        "dbtp_delta_db": measured.true_peak_dbtp - reference_dbtp,
        "rms_db": measured.rms_db,
    }
    if connection is not None and audio_root is not None:
        row["mik_song_overall_volume_control"] = _mik_volume(
            connection, path.relative_to(audio_root)
        )
    return row


def _load_manifest(manifest_path: Path) -> dict[str, str]:
    try:
        raw = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"--manifest {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise SystemExit(
            f"--manifest {manifest_path}: expected a JSON object of "
            "{path: sha256hex}"
        )
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--audio-root", type=Path)
    parser.add_argument("--mik-db", type=Path)
    parser.add_argument(
        "--manifest", type=Path, required=True,
        help="JSON {path: sha256} pinning every input file's bytes; a round "
        "with a fixture missing from or mismatching this manifest is not "
        "acceptance evidence and refuses to run.",
    )
    args = parser.parse_args(argv)
    if (args.audio_root is None) != (args.mik_db is None):
        parser.error("--audio-root and --mik-db must be supplied together")
    manifest = _load_manifest(args.manifest)
    connection: sqlite3.Connection | None = None
    if args.mik_db is not None:
        db_path = args.mik_db.resolve()
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = [_score_file(path, args.audio_root, connection, manifest) for path in args.files]
    failures = [
        row for row in rows
        if abs(row["lufs_delta_lu"]) > LUFS_TOLERANCE_LU
        or abs(row["dbtp_delta_db"]) > TRUE_PEAK_TOLERANCE_DB
    ]
    print(json.dumps(
        {
            "scorer_version": SCORER_VERSION,
            "reference_dependencies": REFERENCE_DEPENDENCIES,
            "rows": rows,
        },
        indent=2,
    ))
    print(
        f"loudness round: fixtures={len(rows)} lufs_failures="
        f"{sum(abs(row['lufs_delta_lu']) > LUFS_TOLERANCE_LU for row in rows)} "
        f"dbtp_failures={sum(abs(row['dbtp_delta_db']) > TRUE_PEAK_TOLERANCE_DB for row in rows)}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
