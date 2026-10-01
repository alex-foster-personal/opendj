"""The coverage drain's per-track jobs: lyrics and vocals.

Split out of ``coverage_drain`` (file-size ceiling); the drain re-exports
every name here, so callers keep importing them from there.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from apps.lyrics.service import LyricsFetchService, load_track

VOCALS_JOB_TIMEOUT_S: float = 300.0
AF_SERVICE_ID: str = "com.af.music-dj-tools.coverage-drain"

JobFn = Callable[[str, str], None]


class NoSource(Exception):
    """A job's answer that there is nothing to make for this track. Terminal."""


def lyrics_job(service: LyricsFetchService, runs: list[str] | None = None) -> JobFn:
    """Fetch lyrics for one track through the shared fetch/cache path.

    The service records ``instrumental`` / ``no_source`` verdicts itself; the
    coverage snapshot reads those, so they need no second record here.
    """

    def run(stable_id: str, _audio_path: str) -> None:
        if runs is not None:
            runs.append(stable_id)
        try:
            track = load_track(service.data_dir / "state" / "state.db", stable_id)
        except ValueError as error:
            # No artist, title or duration to look the track up by.
            raise NoSource(str(error)) from error
        service.fetch_or_resolve(track)

    return run


def vocals_capability_refusal() -> str | None:
    """Why this install cannot derive vocals from stems, or None when it can."""
    if importlib.util.find_spec("soundfile") is None:
        return "soundfile is not installed in the engine environment (analysis extra)"
    return None


def vocals_job(data_dir: Path, stem_roots: Sequence[Path]) -> JobFn:
    """Derive one vocal-cache entry in a niced child process.

    A child, not a thread: reading and reducing a full-length stem is CPU
    work that would otherwise hold the engine's interpreter lock.
    """

    def run(stable_id: str, audio_path: str) -> None:
        argv = [
            sys.executable, "-m", "apps.webui.server.coverage_vocals_job",
            "--data-dir", str(data_dir), "--stable-id", stable_id, "--audio-path", audio_path,
        ]
        for root in stem_roots:
            argv += ["--stem-root", str(root)]
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=VOCALS_JOB_TIMEOUT_S,
            env={**os.environ, "AF_SERVICE_ID": f"{AF_SERVICE_ID}.vocals"},
            check=False,
        )
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout).strip().splitlines()[-1:]
            raise RuntimeError(
                f"vocals from-stems exited {completed.returncode}: {' '.join(tail)}"
            )

    return run


__all__ = [
    "AF_SERVICE_ID",
    "VOCALS_JOB_TIMEOUT_S",
    "JobFn",
    "NoSource",
    "lyrics_job",
    "vocals_capability_refusal",
    "vocals_job",
]
