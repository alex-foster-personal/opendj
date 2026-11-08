"""Read an M3U or M3U8 playlist into absolute track paths (DJIMPORT-10225).

Plain and extended M3U: ``#EXTM3U``, ``#EXTINF`` and every other ``#`` line are directives or
comments, blank lines are skipped, and each remaining line is one track. Relative paths resolve
against the playlist file's own folder; ``file://`` URIs are decoded; CRLF, LF and a UTF-8 BOM are
all accepted. Matching the paths to the collection is the caller's job.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname


def _file_uri_to_path(uri: str) -> str:
    """A ``file://`` URI as a native path: keeps a Windows drive letter and a UNC host."""
    parsed = urlparse(uri)
    host = "" if parsed.netloc.lower() in ("", "localhost") else parsed.netloc
    return os.path.normpath(url2pathname(f"//{host}{parsed.path}" if host else parsed.path))


def _line_to_path(line: str, base_dir: Path) -> str:
    if line.lower().startswith("file://"):
        return _file_uri_to_path(line)
    candidate = Path(line)
    return os.path.normpath(candidate if candidate.is_absolute() else base_dir / candidate)


def parse_m3u(text: str, base_dir: Path) -> list[str]:
    """Absolute, normalised track paths in file order; ``base_dir`` must be absolute."""
    if not base_dir.is_absolute():
        raise ValueError(f"base_dir must be absolute, got {base_dir}")
    paths: list[str] = []
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        paths.append(_line_to_path(line, base_dir))
    return paths


def read_m3u_file(path: Path) -> list[str]:
    """Track paths from an M3U or M3U8 file, relative lines resolved against its folder."""
    return parse_m3u(path.read_text(encoding="utf-8-sig"), path.resolve().parent)
