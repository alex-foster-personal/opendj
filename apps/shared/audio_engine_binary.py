"""Where the ``odj-audio`` engine binary is, by one rule for every caller.

Lives in ``apps.shared`` so a domain package (waveform decode) can find the
same binary the playback supervisor runs without importing ``apps.engine_core``,
which sits above every domain package in the import layers (``.importlinter``).
``apps.engine_core.audio_engine`` re-exports these names.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

BIN_ENV: str = "ODJ_AUDIO_BIN"

#: Relative to the repo root: where ``cargo build`` puts the binary.
REPO_TARGET: Path = Path("apps/audio-engine/target")
EXE_NAME: str = "odj-audio.exe" if os.name == "nt" else "odj-audio"


class AudioEngineError(RuntimeError):
    """A start that cannot go ahead. ``code`` is the wire error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Binary:
    path: Path
    #: ``env`` (ODJ_AUDIO_BIN, the packaged app) or ``repo-release`` /
    #: ``repo-debug`` (a local cargo build in a checkout).
    source: str


def resolve_binary(environ: Mapping[str, str], repo_root: Path) -> Binary:
    """Find the engine binary, or raise ``AudioEngineError('unavailable')``.

    ``ODJ_AUDIO_BIN`` wins and is never second-guessed: when it is set but
    wrong, that is the answer, because a packaged app must never fall back to
    a repo build. Without it, the newest of the release and debug cargo builds
    is used, so a fresh debug build is not shadowed by a stale release one.
    """
    raw = environ.get(BIN_ENV, "").strip()
    if raw:
        p = Path(raw)
        if not p.is_file():
            raise AudioEngineError("unavailable", f"{BIN_ENV}={raw} is not a file")
        if not os.access(p, os.X_OK):
            raise AudioEngineError("unavailable", f"{BIN_ENV}={raw} is not executable")
        return Binary(p, "env")
    found: list[tuple[float, Binary]] = []
    for profile in ("release", "debug"):
        p = repo_root / REPO_TARGET / profile / EXE_NAME
        if p.is_file() and os.access(p, os.X_OK):
            found.append((p.stat().st_mtime, Binary(p, f"repo-{profile}")))
    if not found:
        raise AudioEngineError(
            "unavailable",
            f"no {BIN_ENV} and no local build; run "
            "`cargo build --release --manifest-path apps/audio-engine/Cargo.toml`",
        )
    return max(found, key=lambda t: t[0])[1]
