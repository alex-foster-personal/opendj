"""Library integrity check — guard against the "tracks not in expected location" failure.

Mini-PRD / requirements (status: ✔︎ ✅ done + ran + works):

R1. Given the Rekordbox library, classify every non-streaming track as
    PRESENT / REHOMABLE / MISSING.
      - PRESENT: FolderPath resolves to an existing file.
      - REHOMABLE: file absent at FolderPath, but present after rewriting the
        leading ``/Users/<someone>/`` home prefix to the current ``$HOME`` —
        i.e. a pure migration/path-prefix break that we *could* auto-fix.
      - MISSING: not found at FolderPath nor after re-homing — the bytes are
        genuinely not on this machine.
      [if 9,766/9,824 files are absent on disk then missing_ratio≈0.994 ⛔️]
      [if a file lives at $HOME instead of /Users/dev then REHOMABLE ⛔️]

R2. Expose a single ``assert_healthy()`` invariant for tests/CI/pre-sync:
    broken_ratio (missing+rehomable over tracks-with-paths) must be <= a
    threshold, else raise with a precise, actionable message.
      [if broken_ratio 0.99 and threshold 0.02 then assert_healthy raises ⛔️]
      [if every track resolves then assert_healthy is a no-op ⛔️]

R3. Pure, injectable core (``check_integrity`` takes an iterable of tracks +
    a path-existence predicate) so unit tests are deterministic and never
    touch the real DB. CLI wires it to the live working-copy DB.

Read-only: never mutates any DB. CLI refreshes the working copy via
``paths.copy_live_dbs`` then reads through ``rekordbox_db.open_db``.
"""
from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable, Iterable, Protocol

__all__ = [
    "TrackLike",
    "IntegrityReport",
    "LibraryIntegrityError",
    "rehome_path",
    "check_integrity",
    "assert_healthy",
    "live_report",
]

#: Matches a leading POSIX home prefix, tolerating Rekordbox's ``//`` and any
#: username: ``//Users/dev/Music/x`` -> capture group 1 = ``Music/x``.
_HOME_PREFIX_RE = re.compile(r"^/{1,2}Users/[^/]+/(.*)$")

#: Default invariant ceiling. A healthy library should have ~zero broken links;
#: 2% tolerates a handful of legitimately-removed files between cleanups.
DEFAULT_THRESHOLD: float = 0.02


class TrackLike(Protocol):
    """Structural type for the fields we read off ``rekordbox_db.RBTrack``."""

    folder_path: str
    is_streaming: bool


def rehome_path(folder_path: str, home: str | None = None) -> str | None:
    """Return ``folder_path`` with its ``/Users/<x>/`` prefix swapped to ``home``.

    Returns None when the path has no recognisable home prefix (nothing to
    rewrite). ``home`` defaults to the current ``$HOME``.
    """
    home = home if home is not None else os.path.expanduser("~")
    m = _HOME_PREFIX_RE.match(folder_path or "")
    if not m:
        return None
    posix_home = PurePosixPath(home)
    if posix_home.is_absolute():
        return str(posix_home / PurePosixPath(m.group(1)))
    windows_home = PureWindowsPath(home)
    if windows_home.is_absolute():
        return str(windows_home / PureWindowsPath(m.group(1)))
    raise ValueError(
        f"home must be an absolute POSIX or Windows path, got {home!r}."
    )


@dataclass(slots=True)
class IntegrityReport:
    total: int = 0
    streaming: int = 0
    with_path: int = 0
    present: int = 0
    rehomable: int = 0
    missing: int = 0
    #: Sample of genuinely-missing paths (capped) for fast triage.
    missing_examples: list[str] = field(default_factory=list)
    #: Distinct stale home prefixes seen on broken rows, e.g. ``/Users/dev``.
    stale_home_prefixes: dict[str, int] = field(default_factory=dict)

    @property
    def broken(self) -> int:
        return self.missing + self.rehomable

    @property
    def broken_ratio(self) -> float:
        return self.broken / self.with_path if self.with_path else 0.0

    @property
    def missing_ratio(self) -> float:
        return self.missing / self.with_path if self.with_path else 0.0

    @property
    def rehomable_ratio(self) -> float:
        return self.rehomable / self.with_path if self.with_path else 0.0


class LibraryIntegrityError(AssertionError):
    """Raised by :func:`assert_healthy` when broken_ratio exceeds the threshold."""


def check_integrity(
    tracks: Iterable[TrackLike],
    *,
    exists: Callable[[str], bool] = os.path.exists,
    home: str | None = None,
    max_examples: int = 10,
) -> IntegrityReport:
    """Classify every track. Pure given ``exists`` + ``home`` (injectable for tests)."""
    rep = IntegrityReport()
    for t in tracks:
        rep.total += 1
        if getattr(t, "is_streaming", False):
            rep.streaming += 1
            continue
        path = (t.folder_path or "").strip()
        if not path:
            continue
        rep.with_path += 1
        if exists(path):
            rep.present += 1
            continue
        rehomed = rehome_path(path, home=home)
        if rehomed is not None and exists(rehomed):
            rep.rehomable += 1
        else:
            rep.missing += 1
            if len(rep.missing_examples) < max_examples:
                rep.missing_examples.append(path)
        m = _HOME_PREFIX_RE.match(path)
        if m:
            prefix = path[: m.start(1)].rstrip("/")
            rep.stale_home_prefixes[prefix] = rep.stale_home_prefixes.get(prefix, 0) + 1
    return rep


def assert_healthy(
    report: IntegrityReport, *, threshold: float = DEFAULT_THRESHOLD
) -> None:
    """Raise :class:`LibraryIntegrityError` if too many tracks fail to resolve.

    ``threshold`` is the maximum allowed fraction of broken tracks and must be
    in the range [0.0, 1.0]. We validate explicitly (rather than clamp) so a
    fat-fingered CLI value like ``--threshold=10`` fails loudly instead of
    silently disabling the guard.
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(
            f"Invalid threshold {threshold!r}; expected a fraction in [0.0, 1.0]."
        )
    if report.with_path == 0:
        return
    if report.broken_ratio > threshold:
        top = sorted(
            report.stale_home_prefixes.items(), key=lambda kv: kv[1], reverse=True
        )[:3]
        hint = ", ".join(f"{p} (x{n})" for p, n in top) or "n/a"
        raise LibraryIntegrityError(
            f"Library integrity FAILED: {report.broken}/{report.with_path} tracks "
            f"do not resolve to files on disk "
            f"(broken_ratio={report.broken_ratio:.1%} > threshold={threshold:.1%}). "
            f"missing={report.missing} ({report.missing_ratio:.1%}), "
            f"rehomable={report.rehomable} ({report.rehomable_ratio:.1%}). "
            f"Top stale home prefixes: {hint}. "
            f"Examples missing: {report.missing_examples[:3]}"
        )


# ----- CLI ----------------------------------------------------------------


def _live_report() -> IntegrityReport:
    from apps.shared import paths, rekordbox_db  # lazy: keep core import-light

    copied = paths.copy_live_dbs()
    snapshot = copied.get("rekordbox")
    if snapshot is None:
        raise FileNotFoundError(
            "Unable to create a fresh Rekordbox snapshot: "
            f"live DB is missing at {paths.REKORDBOX_LIVE_DB}."
        )
    db = rekordbox_db.open_db(snapshot)
    try:
        return check_integrity(rekordbox_db.iter_tracks(db))
    finally:
        try:
            db.close()
        except Exception as exc:  # noqa: BLE001
            # Don't mask a real DB/FS problem behind a silent pass — surface
            # it. We warn rather than raise so a close hiccup can't suppress a
            # successfully-built report on the happy path.
            print(f"warning: failed to close Rekordbox DB cleanly: {exc!r}")


def live_report() -> IntegrityReport:
    """Public entry point for write paths that must gate on library health.

    Exists so an apply path can call the guard without reaching for a private
    name; the module used to be reachable only from its own CLI, which is how
    the guard ended up with zero production callers.
    """
    return _live_report()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.audit.library_integrity",
        description="Check that Rekordbox tracks resolve to files on disk.",
    )
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD,
        help=f"max acceptable broken_ratio (default {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="exit non-zero when broken_ratio exceeds --threshold.",
    )
    args = parser.parse_args(argv)

    rep = _live_report()
    print(
        f"tracks={rep.total} streaming={rep.streaming} with_path={rep.with_path}\n"
        f"  present={rep.present} ({rep.present / max(rep.with_path,1):.1%})\n"
        f"  rehomable={rep.rehomable} ({rep.rehomable_ratio:.1%})  "
        f"<- fixable by re-homing to $HOME\n"
        f"  missing={rep.missing} ({rep.missing_ratio:.1%})  "
        f"<- bytes not on this machine\n"
        f"  broken_ratio={rep.broken_ratio:.1%}  threshold={args.threshold:.1%}"
    )
    if rep.stale_home_prefixes:
        print("  stale home prefixes:")
        for p, n in sorted(rep.stale_home_prefixes.items(), key=lambda kv: -kv[1])[:5]:
            print(f"    {n:6d}  {p}")
    if args.strict:
        try:
            assert_healthy(rep, threshold=args.threshold)
        except LibraryIntegrityError as exc:
            print(f"\nFAIL: {exc}")
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
