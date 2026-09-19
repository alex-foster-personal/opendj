"""USB verify engine + CLI.

Hash every file on the drive. Classify each as:

* ``OK``        -- expected + bytes match.
* ``MISSING``   -- expected but not present on drive.
* ``EXTRA``     -- on drive but not expected (and no RENAMED match).
* ``CORRUPTED`` -- on drive, hash differs from canonical.
* ``RENAMED``   -- on drive at an unexpected path, but its hash matches
  some canonical track's ``content_hash``.

Also sanity-checks every ``.m3u8`` under ``<drive>/Playlists/``: every
relative line must resolve to a file classified OK.

CLI usage::

    python -m apps.sync.usb.verify --profile usb-profiles/example.yaml
    python -m apps.sync.usb.verify --profile ... --json data/usb/verify.json
    python -m apps.sync.usb.verify --profile ... --only-drift
    python -m apps.sync.usb.verify --profile ... --skip-playlists
    python -m apps.sync.usb.verify --pioneer-export /Volumes/STICK
    python -m apps.sync.usb.verify --pioneer-export /Volumes/STICK --expected expected.json

Exit codes
----------

Hash mode:

* 0 -- everything OK.
* 2 -- profile load error.
* 3 -- preflight failure.
* 5 -- drift reported.

Pioneer export mode (``--pioneer-export``):

* 0 -- rekordbox export; no ``--expected`` or every expected field matches.
* 2 -- path missing / not a Pioneer tree.
* 4 -- not a rekordbox export (overlay-only OneLibrary).
* 5 -- expected given and at least one ABSENT or MISMATCH.
"""
from __future__ import annotations

import argparse
import concurrent.futures as _cf
import enum
import json
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from rich.console import Console
from rich.table import Table

from apps.shared.hashing import HashCache, sha256_file
from apps.sync.usb import profile as profile_mod
from apps.sync.usb.diff import (
    Op,
    Plan,
    compute_plan,
    enumerate_drive_files,
)
from apps.sync.usb.layout import dst_relpath
from apps.sync.usb.playlist_writer import PLAYLISTS_DIRNAME
from apps.sync.usb.preflight import ALL_CHECKS, preflight
from apps.sync.usb.state import (
    CanonicalTrack,
    group_by_playlist,
    load_canonical_tracks,
)

console = Console(width=120)


class FileStatus(enum.Enum):
    OK = "ok"
    MISSING = "missing"
    EXTRA = "extra"
    CORRUPTED = "corrupted"
    RENAMED = "renamed"


@dataclass(slots=True, frozen=True)
class FileReport:
    status: FileStatus
    stable_id: str | None
    expected_path: str | None        # drive-relative (posix)
    actual_path: str | None          # drive-relative (posix)
    expected_hash: str | None
    actual_hash: str | None
    note: str = ""


@dataclass(slots=True)
class VerifyReport:
    profile_name: str
    drive_root: Path
    drive_uuid: str | None
    files: list[FileReport] = field(default_factory=list)
    ok: int = 0
    missing: int = 0
    extra: int = 0
    corrupted: int = 0
    renamed: int = 0
    playlists_ok: int = 0
    playlists_broken: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def drift_count(self) -> int:
        return self.missing + self.extra + self.corrupted + self.renamed


def _hash_parallel(
    paths: Iterable[Path],
    cache: HashCache | None,
    *,
    max_workers: int | None = None,
) -> dict[Path, str]:
    """Hash every file in ``paths`` (using the cache where possible)."""
    todo: list[Path] = []
    hits: dict[Path, str] = {}
    if cache is not None:
        for p in paths:
            cached = cache.get(p)
            if cached is not None:
                hits[p] = cached
            else:
                todo.append(p)
    else:
        todo = list(paths)

    if not todo:
        return hits

    import os as _os

    workers = max_workers if max_workers else min(8, (_os.cpu_count() or 2))

    def _work(p: Path) -> tuple[Path, str]:
        return p, sha256_file(p)

    # Workers return tuples; the pool writes to ``hits`` (main thread only,
    # via the _cf.map iterator) and the cache write is deferred until after
    # the pool has joined. HashCache wraps a sqlite3.Connection that is
    # bound to the thread that created it (check_same_thread defaults to
    # True); performing put() only on the main thread after the pool exits
    # makes the thread-safety contract explicit and defends against future
    # refactors that might move the call inside ``_work``.
    results: list[tuple[Path, str]] = []
    with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for path, digest in pool.map(_work, todo):
            hits[path] = digest
            results.append((path, digest))
    if cache is not None:
        for path, digest in results:
            try:
                cache.put(path, digest)
            except OSError:
                pass
    return hits


def _expected_paths(
    *,
    profile: profile_mod.Profile,
    canonical: list[CanonicalTrack],
) -> dict[PurePosixPath, CanonicalTrack]:
    """Compute the expected (dst_rel -> CanonicalTrack) map."""
    out: dict[PurePosixPath, CanonicalTrack] = {}
    grouped = group_by_playlist(canonical)
    for playlist_name, tracks in grouped.items():
        for idx, t in enumerate(tracks, start=1):
            rel = dst_relpath(
                layout=profile.layout,
                format_=profile.format,
                playlist=playlist_name,
                track_index=idx,
                artist=t.artist,
                album=t.album,
                title=t.title,
                src=t.source_path,
            )
            # First-wins on collision.
            if rel not in out:
                out[rel] = t
    return out


def verify_drive(
    *,
    profile: profile_mod.Profile,
    canonical: list[CanonicalTrack],
    drive_root: Path,
    hash_cache: HashCache | None = None,
    skip_playlists: bool = False,
    detect_renames: bool = True,
    max_workers: int | None = None,
) -> VerifyReport:
    """Build a :class:`VerifyReport` against the drive at ``drive_root``."""
    t0 = time.monotonic()

    expected = _expected_paths(profile=profile, canonical=canonical)
    expected_hashes = {str(t.stable_id): t for t in canonical}
    expected_hash_to_track: dict[str, CanonicalTrack] = {}
    for t in canonical:
        # A content_hash is per-file; dupes are fine (first wins).
        expected_hash_to_track.setdefault(t.content_hash, t)

    drive_files = enumerate_drive_files(drive_root)
    drive_rels: dict[PurePosixPath, Path] = {
        PurePosixPath(p.relative_to(drive_root).as_posix()): p for p in drive_files
    }

    # Hash drive files (parallel) + expected sources (opt-out: we already
    # have content_hash from state.load).
    drive_hashes = _hash_parallel(
        drive_files, hash_cache, max_workers=max_workers
    )

    report = VerifyReport(
        profile_name=profile.name,
        drive_root=drive_root,
        drive_uuid=None,
    )

    expected_rels = set(expected.keys())
    actual_rels = set(drive_rels.keys())

    # -- MISSING: expected but absent ---------------------------------------
    for rel in sorted(expected_rels - actual_rels, key=str):
        track = expected[rel]
        report.files.append(
            FileReport(
                status=FileStatus.MISSING,
                stable_id=track.stable_id,
                expected_path=str(rel),
                actual_path=None,
                expected_hash=track.content_hash,
                actual_hash=None,
                note="canonical expects, drive does not have",
            )
        )
        report.missing += 1

    # -- OK / CORRUPTED ------------------------------------------------------
    for rel in sorted(expected_rels & actual_rels, key=str):
        track = expected[rel]
        actual = drive_hashes.get(drive_rels[rel], "")
        if actual == track.content_hash:
            report.files.append(
                FileReport(
                    status=FileStatus.OK,
                    stable_id=track.stable_id,
                    expected_path=str(rel),
                    actual_path=str(rel),
                    expected_hash=track.content_hash,
                    actual_hash=actual,
                )
            )
            report.ok += 1
        else:
            report.files.append(
                FileReport(
                    status=FileStatus.CORRUPTED,
                    stable_id=track.stable_id,
                    expected_path=str(rel),
                    actual_path=str(rel),
                    expected_hash=track.content_hash,
                    actual_hash=actual,
                    note="hash differs from canonical",
                )
            )
            report.corrupted += 1

    # -- EXTRA / RENAMED -----------------------------------------------------
    extras = sorted(actual_rels - expected_rels, key=str)
    for rel in extras:
        actual_path = drive_rels[rel]
        actual_hash = drive_hashes.get(actual_path, "")
        if detect_renames and actual_hash in expected_hash_to_track:
            track = expected_hash_to_track[actual_hash]
            # Where does canonical think this track belongs?
            expected_rel: str | None = None
            for er, t in expected.items():
                if t.stable_id == track.stable_id:
                    expected_rel = str(er)
                    break
            report.files.append(
                FileReport(
                    status=FileStatus.RENAMED,
                    stable_id=track.stable_id,
                    expected_path=expected_rel,
                    actual_path=str(rel),
                    expected_hash=track.content_hash,
                    actual_hash=actual_hash,
                    note="bytes match canonical but path differs",
                )
            )
            report.renamed += 1
        else:
            report.files.append(
                FileReport(
                    status=FileStatus.EXTRA,
                    stable_id=None,
                    expected_path=None,
                    actual_path=str(rel),
                    expected_hash=None,
                    actual_hash=actual_hash,
                    note="not in profile",
                )
            )
            report.extra += 1

    # -- playlist sanity -----------------------------------------------------
    if not skip_playlists:
        pl_root = drive_root / PLAYLISTS_DIRNAME
        if pl_root.exists():
            for m3u8 in sorted(pl_root.glob("*.m3u8")):
                # Codex finding P10-F02: the pre-fix check only asserted
                # that entry lines *resolved*, so a truncated/corrupted
                # playlist (empty file, missing #EXTM3U header, or a
                # file with zero entry lines) silently passed as healthy.
                # We now require a valid header AND at least one resolved
                # entry; any unresolvable entry classifies the playlist
                # as broken.
                try:
                    text = m3u8.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    report.playlists_broken.append(m3u8.name)
                    continue
                lines = text.splitlines()
                # Find the first non-blank line; it must be the #EXTM3U
                # header per the playlist writer's output contract.
                header: str | None = None
                for ln in lines:
                    stripped = ln.strip()
                    if stripped:
                        header = stripped
                        break
                if header != "#EXTM3U":
                    report.playlists_broken.append(m3u8.name)
                    continue
                broken = False
                entry_count = 0
                drive_resolved = drive_root.resolve()
                for line in lines:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Resolve relative to the m3u8's dir.
                    try:
                        target = (m3u8.parent / line).resolve()
                    except OSError:
                        broken = True
                        break
                    try:
                        target.relative_to(drive_resolved)
                    except ValueError:
                        broken = True
                        break
                    if not target.exists():
                        broken = True
                        break
                    entry_count += 1
                if broken or entry_count == 0:
                    report.playlists_broken.append(m3u8.name)
                else:
                    report.playlists_ok += 1

    report.duration_seconds = round(time.monotonic() - t0, 3)
    return report


def plan_from_verify(
    *,
    profile: profile_mod.Profile,
    canonical: list[CanonicalTrack],
    report: VerifyReport,
    drive_root: Path,
    restrict_statuses: set[FileStatus] | None = None,
) -> Plan:
    """Build a remediation :class:`Plan` from ``report``.

    * MISSING   -> copy
    * CORRUPTED -> overwrite
    * EXTRA     -> delete
    * RENAMED   -> rename (uses ``Op.src = actual path on drive``)
    """
    # Start from an empty, canonical-backed plan so we can pick pieces out.
    base = compute_plan(profile=profile, canonical=canonical, drive_root=drive_root)
    expected_by_rel = {op.dst_rel: op for op in base.ops if op.kind in ("copy", "transcode", "overwrite")}

    # Build lookups.
    grouped = group_by_playlist(canonical)
    expected = _expected_paths(profile=profile, canonical=canonical)

    filtered: list[Op] = []
    for f in report.files:
        if restrict_statuses is not None and f.status not in restrict_statuses:
            continue
        if f.status is FileStatus.MISSING and f.expected_path:
            rel = PurePosixPath(f.expected_path)
            track = expected.get(rel)
            if track is None:
                continue
            kind = "transcode" if profile.needs_transcode else "copy"
            filtered.append(
                Op(
                    kind=kind,
                    dst=drive_root / rel,
                    dst_rel=rel,
                    src=track.source_path,
                    stable_id=track.stable_id,
                    expected_hash=track.content_hash,
                    reason="remediate: missing",
                    bytes_estimate=track.size_bytes,
                )
            )
        elif f.status is FileStatus.CORRUPTED and f.expected_path:
            rel = PurePosixPath(f.expected_path)
            track = expected.get(rel)
            if track is None:
                continue
            filtered.append(
                Op(
                    kind="overwrite",
                    dst=drive_root / rel,
                    dst_rel=rel,
                    src=track.source_path,
                    stable_id=track.stable_id,
                    expected_hash=track.content_hash,
                    reason="remediate: corrupted",
                    bytes_estimate=track.size_bytes,
                )
            )
        elif f.status is FileStatus.EXTRA and f.actual_path:
            rel = PurePosixPath(f.actual_path)
            filtered.append(
                Op(
                    kind="delete",
                    dst=drive_root / rel,
                    dst_rel=rel,
                    src=None,
                    stable_id=None,
                    expected_hash=None,
                    reason="remediate: extra",
                    bytes_estimate=0,
                )
            )
        elif f.status is FileStatus.RENAMED and f.expected_path and f.actual_path:
            rel_new = PurePosixPath(f.expected_path)
            rel_old = PurePosixPath(f.actual_path)
            track = expected.get(rel_new)
            if track is None:
                continue
            filtered.append(
                Op(
                    kind="rename",
                    dst=drive_root / rel_new,
                    dst_rel=rel_new,
                    src=drive_root / rel_old,
                    stable_id=track.stable_id,
                    expected_hash=track.content_hash,
                    reason="remediate: renamed",
                    bytes_estimate=0,
                )
            )

    total_bytes = sum(
        op.bytes_estimate
        for op in filtered
        if op.kind in ("copy", "transcode", "overwrite")
    )
    return Plan(
        profile_name=profile.name,
        drive_root=drive_root,
        ops=filtered,
        total_bytes=total_bytes,
        existing_bytes=base.existing_bytes,
        free_bytes_needed=int(total_bytes * 1.10),
        warnings=[],
        tracks_by_playlist=grouped,
    )


# ----------------------------------------------------------------------- CLI


def report_to_jsonable(report: VerifyReport) -> dict:
    return {
        "profile_name": report.profile_name,
        "drive_root": str(report.drive_root),
        "drive_uuid": report.drive_uuid,
        "counts": {
            "ok": report.ok,
            "missing": report.missing,
            "extra": report.extra,
            "corrupted": report.corrupted,
            "renamed": report.renamed,
            "playlists_ok": report.playlists_ok,
            "playlists_broken": len(report.playlists_broken),
        },
        "duration_seconds": report.duration_seconds,
        "playlists_broken": report.playlists_broken,
        "files": [
            {
                "status": f.status.value,
                "stable_id": f.stable_id,
                "expected_path": f.expected_path,
                "actual_path": f.actual_path,
                "expected_hash": f.expected_hash,
                "actual_hash": f.actual_hash,
                "note": f.note,
            }
            for f in report.files
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.sync.usb.verify",
        description="Verify drive contents against canonical (SHA-256).",
    )
    p.add_argument("--profile", required=False, default=None)
    p.add_argument(
        "--pioneer-export",
        dest="pioneer_export",
        default=None,
        help="Rekordbox-exported Pioneer tree (USB root or PIONEER/ dir)",
    )
    p.add_argument(
        "--expected",
        default=None,
        help="Optional JSON of expected stick values (Pioneer export mode)",
    )
    p.add_argument(
        "--drive-root",
        default=None,
        help="Drive mount path (defaults from the profile label on macOS; required elsewhere)",
    )
    p.add_argument("--json", dest="json_out", default=None)
    p.add_argument("--only-drift", action="store_true")
    p.add_argument("--skip-playlists", action="store_true")
    p.add_argument("--no-detect-renames", action="store_true")
    p.add_argument(
        "--from-shared-state",
        action="store_true",
    )
    p.add_argument(
        "--skip-check",
        action="append",
        default=[],
        choices=list(ALL_CHECKS),
    )
    p.add_argument("--verbose", action="store_true")
    return p


def _main_pioneer_export(args: argparse.Namespace) -> int:
    from apps.sync.usb.pioneer.value_verify import (
        load_expected_json,
        pioneer_export_exit_code,
        stick_values_to_jsonable,
        verify_stick_values,
    )

    pioneer_path = Path(args.pioneer_export)
    if not pioneer_path.exists():
        console.print(f"[red]path not found: {pioneer_path}[/red]")
        return 2

    expected = None
    if args.expected:
        expected = load_expected_json(Path(args.expected))

    try:
        report = verify_stick_values(pioneer_path, expected=expected)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    if not report.is_rekordbox_export:
        console.print(f"[yellow]{report.overlay_note}[/yellow]")
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(
                json.dumps(stick_values_to_jsonable(report), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return 4

    console.print(f"tracks on stick: {report.tracks_on_stick}")
    console.print(f"({report.denominator_label})")

    table = Table(title="stick values")
    table.add_column("field")
    for col in ("present", "absent", "match", "mismatch", "unread"):
        table.add_column(col, justify="right")
    for label, counts in (
        ("key", report.key),
        ("loudness", report.loudness),
        ("grid", report.grid),
    ):
        table.add_row(
            label,
            str(counts.present),
            str(counts.absent),
            str(counts.match),
            str(counts.mismatch),
            str(counts.unread),
        )
    console.print(table)

    if report.unread_reasons:
        for reason in report.unread_reasons:
            console.print(f"[dim]unread: {reason}[/dim]")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(stick_values_to_jsonable(report), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        console.print(f"[green]wrote {args.json_out}[/green]")

    return pioneer_export_exit_code(report, has_expected=expected is not None)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.pioneer_export:
        return _main_pioneer_export(args)
    if not args.profile:
        _build_parser().error("--profile is required when --pioneer-export is not set")
    try:
        profile = profile_mod.load(args.profile)
        drive_root = Path(args.drive_root) if args.drive_root else profile.mount_point
    except FileNotFoundError as exc:
        console.print(f"[red]profile not found: {exc}[/red]")
        return 2
    except profile_mod.ProfileError as exc:
        console.print(f"[red]profile error: {exc}[/red]")
        return 2

    canonical = load_canonical_tracks(
        playlist_names=profile.playlists,
        use_shared_state=args.from_shared_state,
    )

    # Preflight without write_probe (verify is read-only).
    plan = compute_plan(profile=profile, canonical=canonical, drive_root=drive_root)
    pre = preflight(
        profile,
        plan,
        drive_root=drive_root,
        write_probe=False,
        skip_checks=set(args.skip_check),
    )
    if not pre.ok:
        for err in pre.errors:
            console.print(f"[red]preflight:[/red] {err}")
        return 3

    report = verify_drive(
        profile=profile,
        canonical=canonical,
        drive_root=drive_root,
        skip_playlists=args.skip_playlists,
        detect_renames=not args.no_detect_renames,
    )

    summary = Table(title=f"verify: {profile.name}")
    summary.add_column("status")
    summary.add_column("count", justify="right")
    summary.add_row("ok", str(report.ok))
    summary.add_row("missing", str(report.missing))
    summary.add_row("extra", str(report.extra))
    summary.add_row("corrupted", str(report.corrupted))
    summary.add_row("renamed", str(report.renamed))
    summary.add_row("playlists_ok", str(report.playlists_ok))
    summary.add_row("playlists_broken", str(len(report.playlists_broken)))
    console.print(summary)
    console.print(f"[dim]hashed in {report.duration_seconds}s[/dim]")

    if args.verbose or args.only_drift:
        table = Table(title="per-file")
        table.add_column("status")
        table.add_column("expected")
        table.add_column("actual")
        for f in report.files:
            if args.only_drift and f.status is FileStatus.OK:
                continue
            table.add_row(f.status.value, f.expected_path or "", f.actual_path or "")
        console.print(table)

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(report_to_jsonable(report), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        console.print(f"[green]wrote {args.json_out}[/green]")

    drift = report.drift_count() + len(report.playlists_broken)
    return 0 if drift == 0 else 5


if __name__ == "__main__":
    sys.exit(main())
