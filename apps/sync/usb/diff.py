"""Plan engine: canonical tracks + drive contents -> typed Op list.

Outputs a :class:`Plan` that ``plan`` (dry-run) renders and ``apply``
executes. The diff is cheap: it uses (size, mtime) to decide whether an
existing destination needs rewriting (with a 2-second exFAT tolerance).
Verification uses SHA-256 separately -- see ``apps/sync/usb/verify.py``.

Op kinds
--------

* ``copy``     -- no destination exists; source -> dst.
* ``transcode``-- like ``copy`` but via ffmpeg (profile ``format=mp3@320``).
* ``overwrite``-- destination exists but differs; policy decides whether
  to proceed (canonical-wins / backup-then-overwrite) or to emit a
  ``skip`` warning instead.
* ``delete``   -- drive-only file not mentioned by the profile.
* ``rename``   -- used only by drift remediation (Plan 10-02); the diff
  engine does not emit this by default but callers can compose it.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from apps.sync.usb.layout import dst_relpath
from apps.sync.usb.profile import Profile
from apps.sync.usb.state import CanonicalTrack, group_by_playlist

OpKind = Literal["copy", "transcode", "overwrite", "delete", "rename", "skip"]

# exFAT stores mtime at 2-second granularity. APFS is nanosecond-accurate.
# We treat everything <= 2s as "equal" when deciding if the copy target
# is up-to-date. Source: ``10-RESEARCH.md#4``.
MTIME_TOLERANCE_SECS = 2.0

# Files we never manage: our own marker + Playlists/ + macOS sidecars.
SKIP_PATTERNS: tuple[str, ...] = (
    ".mdj-marker.json",
    "Playlists/",
    ".mdj-export/",
    ".mdj-probe",
)


@dataclass(slots=True, frozen=True)
class Op:
    """One atomic operation in the :class:`Plan`.

    Paths are always absolute for ``src``; ``dst`` is absolute on the
    drive; ``dst_rel`` is drive-relative (``PurePosixPath``). ``delete``
    ops have ``src=None`` and ``stable_id=None``.
    """

    kind: OpKind
    dst: Path
    dst_rel: PurePosixPath
    reason: str
    bytes_estimate: int
    src: Path | None = None
    stable_id: str | None = None
    expected_hash: str | None = None


@dataclass(slots=True)
class Plan:
    profile_name: str
    drive_root: Path
    ops: list[Op]
    total_bytes: int                  # sum of bytes_estimate for copies/overwrites/transcodes
    existing_bytes: int               # already on drive and unchanged
    free_bytes_needed: int            # incremental space needed (headroom included)
    warnings: list[str] = field(default_factory=list)
    tracks_by_playlist: dict[str, list[CanonicalTrack]] = field(default_factory=dict)


def _is_macos_sidecar(p: Path) -> bool:
    return p.name.startswith("._")


def _should_skip(rel: PurePosixPath) -> bool:
    s = str(rel)
    for pat in SKIP_PATTERNS:
        if pat.endswith("/"):
            if s.startswith(pat):
                return True
        elif s == pat:
            return True
    if rel.name.startswith("._"):
        return True
    return False


def enumerate_drive_files(drive_root: Path) -> list[Path]:
    """Return every regular file under ``drive_root`` (excluding skip list)."""
    if not drive_root.exists():
        return []
    out: list[Path] = []
    for p in drive_root.rglob("*"):
        if not p.is_file():
            continue
        rel = PurePosixPath(p.relative_to(drive_root).as_posix())
        if _should_skip(rel):
            continue
        if _is_macos_sidecar(p):
            continue
        out.append(p)
    return out


def _existing_up_to_date(src: Path, dst: Path) -> bool:
    """Size + mtime (with 2s tolerance) match means "no copy needed"."""
    try:
        ss = src.stat()
        ds = dst.stat()
    except OSError:
        return False
    if ss.st_size != ds.st_size:
        return False
    return abs(ss.st_mtime - ds.st_mtime) <= MTIME_TOLERANCE_SECS


def _is_excluded(rel: PurePosixPath, exclusions: Iterable[str]) -> bool:
    s = str(rel)
    for prefix in exclusions:
        if not prefix:
            continue
        if s == prefix or s.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def compute_plan(
    *,
    profile: Profile,
    canonical: list[CanonicalTrack],
    drive_root: Path,
) -> Plan:
    """Build a :class:`Plan` for the given profile + canonical set."""
    ops: list[Op] = []
    warnings: list[str] = []

    # --- 1. Compute per-playlist track_index and dst path for each track.
    grouped = group_by_playlist(canonical)
    expected: dict[PurePosixPath, tuple[CanonicalTrack, int]] = {}
    case_buckets: dict[str, list[PurePosixPath]] = defaultdict(list)

    for playlist, tracks in grouped.items():
        for idx, t in enumerate(tracks, start=1):
            rel = dst_relpath(
                layout=profile.layout,
                format_=profile.format,
                playlist=playlist,
                track_index=idx,
                artist=t.artist,
                album=t.album,
                title=t.title,
                src=t.source_path,
            )
            # Duplicate stable_ids across playlists (layout=Artist/Album/Track
            # is playlist-agnostic): keep the first mapping, warn once.
            if rel in expected:
                warnings.append(
                    f"track duplicated across playlists mapping to {rel}: "
                    f"{t.stable_id}"
                )
                continue
            expected[rel] = (t, idx)
            case_buckets[str(rel).lower()].append(rel)

    # --- 2. Case-collision check.
    for _key, variants in case_buckets.items():
        if len(variants) > 1:
            warnings.append(
                "case collision: "
                + ", ".join(sorted(str(v) for v in variants))
            )

    # --- 3. Existing files on drive.
    existing = enumerate_drive_files(drive_root)
    existing_by_rel: dict[PurePosixPath, Path] = {}
    for p in existing:
        rel = PurePosixPath(p.relative_to(drive_root).as_posix())
        existing_by_rel[rel] = p

    # --- 4. Emit copy / overwrite / skip ops.
    total_bytes = 0
    existing_bytes = 0
    for rel, (track, _idx) in expected.items():
        dst_abs = drive_root / rel
        copy_kind: OpKind = "transcode" if profile.needs_transcode else "copy"
        if rel not in existing_by_rel:
            ops.append(
                Op(
                    kind=copy_kind,
                    dst=dst_abs,
                    dst_rel=rel,
                    src=track.source_path,
                    stable_id=track.stable_id,
                    expected_hash=track.content_hash,
                    reason="new track in profile",
                    bytes_estimate=track.size_bytes,
                )
            )
            total_bytes += track.size_bytes
        else:
            existing_abs = existing_by_rel[rel]
            if _existing_up_to_date(track.source_path, existing_abs):
                existing_bytes += existing_abs.stat().st_size
                continue
            # Drift on drive; honour conflict_policy.
            if profile.conflict_policy == "skip":
                warnings.append(
                    f"skip overwrite (conflict_policy=skip): {rel}"
                )
                ops.append(
                    Op(
                        kind="skip",
                        dst=dst_abs,
                        dst_rel=rel,
                        src=track.source_path,
                        stable_id=track.stable_id,
                        expected_hash=track.content_hash,
                        reason="drift; conflict_policy=skip",
                        bytes_estimate=0,
                    )
                )
                continue
            ops.append(
                Op(
                    kind="overwrite",
                    dst=dst_abs,
                    dst_rel=rel,
                    src=track.source_path,
                    stable_id=track.stable_id,
                    expected_hash=track.content_hash,
                    reason="drift on drive",
                    bytes_estimate=track.size_bytes,
                )
            )
            total_bytes += track.size_bytes

    # --- 5. Emit delete ops for drive-only files (respecting exclusions).
    expected_rels = set(expected.keys())
    for rel, abs_path in existing_by_rel.items():
        if rel in expected_rels:
            continue
        if _is_excluded(rel, profile.exclusions):
            continue
        try:
            sz = abs_path.stat().st_size
        except OSError:
            sz = 0
        ops.append(
            Op(
                kind="delete",
                dst=abs_path,
                dst_rel=rel,
                src=None,
                stable_id=None,
                expected_hash=None,
                reason="not in profile",
                bytes_estimate=sz,
            )
        )

    # --- 6. Free-bytes estimate (10% headroom).
    free_bytes_needed = int(total_bytes * 1.10)

    return Plan(
        profile_name=profile.name,
        drive_root=drive_root,
        ops=ops,
        total_bytes=total_bytes,
        existing_bytes=existing_bytes,
        free_bytes_needed=free_bytes_needed,
        warnings=warnings,
        tracks_by_playlist=grouped,
    )


def filter_plan_by_playlists(plan: Plan, playlists: Iterable[str]) -> Plan:
    """Return a new plan limited to ops whose track belongs to ``playlists``."""
    wanted = set(playlists)
    allowed_stable_ids: set[str] = set()
    for name, tracks in plan.tracks_by_playlist.items():
        if name in wanted:
            allowed_stable_ids.update(t.stable_id for t in tracks)
    filtered = [op for op in plan.ops if op.stable_id in allowed_stable_ids]
    return Plan(
        profile_name=plan.profile_name,
        drive_root=plan.drive_root,
        ops=filtered,
        total_bytes=sum(
            op.bytes_estimate
            for op in filtered
            if op.kind in ("copy", "transcode", "overwrite")
        ),
        existing_bytes=plan.existing_bytes,
        free_bytes_needed=int(
            sum(
                op.bytes_estimate
                for op in filtered
                if op.kind in ("copy", "transcode", "overwrite")
            )
            * 1.10
        ),
        warnings=list(plan.warnings),
        tracks_by_playlist={
            k: v for k, v in plan.tracks_by_playlist.items() if k in wanted
        },
    )


def filter_plan_by_files(plan: Plan, rel_paths: Iterable[str]) -> Plan:
    """Return a new plan restricted to ops whose dst_rel matches ``rel_paths``."""
    wanted = {str(PurePosixPath(rp)).lstrip("/") for rp in rel_paths}
    filtered = [op for op in plan.ops if str(op.dst_rel) in wanted]
    return Plan(
        profile_name=plan.profile_name,
        drive_root=plan.drive_root,
        ops=filtered,
        total_bytes=sum(
            op.bytes_estimate
            for op in filtered
            if op.kind in ("copy", "transcode", "overwrite")
        ),
        existing_bytes=plan.existing_bytes,
        free_bytes_needed=int(
            sum(
                op.bytes_estimate
                for op in filtered
                if op.kind in ("copy", "transcode", "overwrite")
            )
            * 1.10
        ),
        warnings=list(plan.warnings),
        tracks_by_playlist=plan.tracks_by_playlist,
    )


def plan_summary(plan: Plan) -> dict[str, int]:
    """Return counts per op kind for display/tests."""
    counts: dict[str, int] = defaultdict(int)
    for op in plan.ops:
        counts[op.kind] += 1
    counts["total_bytes"] = plan.total_bytes
    counts["existing_bytes"] = plan.existing_bytes
    counts["free_bytes_needed"] = plan.free_bytes_needed
    return dict(counts)


__all__ = [
    "Op",
    "OpKind",
    "Plan",
    "SKIP_PATTERNS",
    "MTIME_TOLERANCE_SECS",
    "enumerate_drive_files",
    "compute_plan",
    "filter_plan_by_playlists",
    "filter_plan_by_files",
    "plan_summary",
]
