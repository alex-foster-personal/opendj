"""POSIX process-tree helpers for vocal worker acceptance tests."""

from __future__ import annotations

import re
from collections.abc import Iterable

import psutil

_WORKER_CMDLINE_RE = re.compile(r"(uv|python|demucs|ffmpeg)", re.IGNORECASE)


def descendants_of(pid: int) -> set[int]:
    """Return every descendant pid of ``pid``, excluding ``pid`` itself."""
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return set()
    out: set[int] = set()
    for child in process.children(recursive=True):
        try:
            out.add(child.pid)
        except psutil.NoSuchProcess:
            continue
    return out


def cmdline_of(pid: int) -> str:
    try:
        parts = psutil.Process(pid).cmdline()
    except psutil.NoSuchProcess:
        return ""
    return " ".join(parts)


def worker_like_pids(pids: Iterable[int]) -> list[int]:
    """Pids whose cmdline matches uv/python/demucs/ffmpeg."""
    return [pid for pid in pids if _WORKER_CMDLINE_RE.search(cmdline_of(pid))]
