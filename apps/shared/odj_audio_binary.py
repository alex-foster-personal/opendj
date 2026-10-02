"""Where the ``odj-audio`` binary is, in the installed app and in a checkout.

One rule, shared by the engine supervisor (``apps.engine_core.audio_engine``)
and the stems and vocals workers' decoder (``apps.shared.odj_audio_decode``),
so they cannot disagree about which engine a process runs:

* ``ODJ_AUDIO_BIN`` wins and is never second-guessed. The payload launcher
  exports it as ``$payload/bin/odj-audio``; when it is set but wrong, that is
  the answer, because an installed app must never fall back to a repo build.
* Without it (a checkout), the newer of the release and debug cargo builds
  under ``apps/audio-engine/target``, so a fresh debug build is not shadowed
  by a stale release one.

Lives in ``apps.shared`` because the workers' side may not import
``apps.engine_core`` (.importlinter: shared is the stable core, and engine_core
sits above every domain package).
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


class OdjAudioUnavailable(RuntimeError):
    """No usable ``odj-audio`` here; the message says why."""


@dataclass(frozen=True)
class Binary:
    path: Path
    #: ``env`` (ODJ_AUDIO_BIN, the packaged app) or ``repo-release`` /
    #: ``repo-debug`` (a local cargo build in a checkout).
    source: str


def find_binary(environ: Mapping[str, str], repo_root: Path) -> Binary:
    """The ``odj-audio`` to run, or :class:`OdjAudioUnavailable` (see module)."""
    raw = environ.get(BIN_ENV, "").strip()
    if raw:
        p = Path(raw)
        if not p.is_file():
            raise OdjAudioUnavailable(f"{BIN_ENV}={raw} is not a file")
        if not os.access(p, os.X_OK):
            raise OdjAudioUnavailable(f"{BIN_ENV}={raw} is not executable")
        return Binary(p, "env")
    found: list[tuple[float, Binary]] = []
    for profile in ("release", "debug"):
        p = repo_root / REPO_TARGET / profile / EXE_NAME
        if p.is_file() and os.access(p, os.X_OK):
            found.append((p.stat().st_mtime, Binary(p, f"repo-{profile}")))
    if not found:
        raise OdjAudioUnavailable(
            f"no {BIN_ENV} and no local build; run "
            "`cargo build --release --manifest-path apps/audio-engine/Cargo.toml`"
        )
    return max(found, key=lambda t: t[0])[1]


__all__ = ["BIN_ENV", "EXE_NAME", "REPO_TARGET", "Binary", "OdjAudioUnavailable", "find_binary"]
