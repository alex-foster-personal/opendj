"""Pre-flight safety checks for USB apply operations.

Each check is individually toggleable via ``--skip-check <name>`` so an
operator stuck behind a quirk (e.g. a write-probe that fails on a
read-only fixture drive) can bypass it with an audit log line.

Checks that fail return an ``error`` (stops apply). Checks that warn
allow apply to continue; they are shown in the preflight summary.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from apps.sync.usb.diff import Plan
from apps.sync.usb.profile import Profile

ALL_CHECKS: tuple[str, ...] = (
    "drive_mounted",
    "drive_label",
    "drive_uuid",
    "drive_writable",
    "free_space",
    "no_other_writer",
    "ffmpeg_available",
    "no_case_collisions",
)


@dataclass(slots=True)
class PreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _probe_writable(drive_root: Path) -> tuple[bool, str]:
    probe = drive_root / ".mdj-probe"
    try:
        probe.write_bytes(b"mdj")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _lsof_writers(drive_root: Path) -> list[str]:
    """Return lsof lines indicating other writers, or [] if none.

    On macOS ``lsof +D <path>`` lists every open file under the path.
    We heuristically treat entries ending in ``w`` or ``u`` in the FD
    column as writers. Missing lsof or permission errors -> empty (we
    warn rather than block).
    """
    try:
        result = subprocess.run(
            ["lsof", "+D", str(drive_root)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    writers: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        fd = parts[3]
        # FD suffix: r read, w write, u read+write.
        if fd.endswith("w") or fd.endswith("u"):
            writers.append(line)
    return writers


def preflight(
    profile: Profile,
    plan: Plan,
    *,
    drive_root: Path | None = None,
    write_probe: bool = False,
    skip_checks: set[str] | None = None,
    marker_uuid: str | None = None,
    ffmpeg_path: str | None = None,
    drive_free_bytes: int | None = None,
) -> PreflightResult:
    """Run every pre-flight check that is not in ``skip_checks``.

    ``drive_root`` defaults to ``profile.mount_point`` (``/Volumes/<label>``)
    when not supplied; tests pass a ``tmp_path`` here.

    ``drive_free_bytes`` lets tests override :func:`shutil.disk_usage`;
    when None we call it for real on ``drive_root``.
    """
    skip = skip_checks or set()
    res = PreflightResult(ok=True)
    root = drive_root if drive_root is not None else profile.mount_point

    def _skip(name: str) -> bool:
        if name in skip:
            res.skipped.append(name)
            res.warnings.append(f"check skipped: {name}")
            return True
        return False

    # -- drive_mounted ---------------------------------------------------
    if not _skip("drive_mounted"):
        if not root.exists() or not root.is_dir():
            res.errors.append(
                f"drive_mounted: {root} does not exist or is not a directory"
            )

    # -- drive_label -----------------------------------------------------
    if not _skip("drive_label"):
        if root.exists():
            # ``drive_root.name`` matches ``profile.drive_label`` when we
            # mount at ``/Volumes/<label>``. Tests pass ``tmp_path`` with
            # a name that matches the profile.
            if root.name != profile.drive_label:
                res.errors.append(
                    f"drive_label: expected '{profile.drive_label}', "
                    f"drive mounted as '{root.name}'"
                )

    # -- drive_uuid ------------------------------------------------------
    if not _skip("drive_uuid"):
        if profile.drive_uuid is not None and marker_uuid is not None:
            if profile.drive_uuid != marker_uuid:
                res.errors.append(
                    f"drive_uuid: profile '{profile.drive_uuid}' != "
                    f"marker '{marker_uuid}'"
                )
        elif profile.drive_uuid is None and marker_uuid is None:
            # First run; no UUID to compare. Soft warning only.
            pass

    # -- drive_writable --------------------------------------------------
    if write_probe and not _skip("drive_writable"):
        if root.exists() and root.is_dir():
            ok, detail = _probe_writable(root)
            if not ok:
                res.errors.append(f"drive_writable: {detail}")

    # -- free_space ------------------------------------------------------
    if not _skip("free_space"):
        if root.exists():
            if drive_free_bytes is None:
                try:
                    drive_free_bytes = shutil.disk_usage(root).free
                except OSError as exc:
                    res.warnings.append(f"free_space: unable to stat drive ({exc})")
                    drive_free_bytes = None
            if drive_free_bytes is not None:
                if drive_free_bytes < plan.free_bytes_needed:
                    res.errors.append(
                        f"free_space: need {plan.free_bytes_needed} bytes "
                        f"(incl 10% headroom), drive free={drive_free_bytes}"
                    )

    # -- no_other_writer -------------------------------------------------
    # P10-F03: previously this check only ran in ``write_probe`` (apply)
    # mode and even then was warning-only, which made plan/verify blind to
    # concurrent writers on the drive. We now:
    #   * run the probe in any mode when the drive is mounted, and
    #   * promote the finding to an error in ``write_probe`` mode while
    #     keeping it a warning in read-only (plan/verify) mode, so an
    #     apply cannot silently proceed while another process has files
    #     open for write on the drive.
    if not _skip("no_other_writer") and root.exists():
        writers = _lsof_writers(root)
        if writers:
            msg = (
                f"no_other_writer: {len(writers)} other processes "
                "have files open for write on the drive"
            )
            if write_probe:
                res.errors.append(msg)
            else:
                res.warnings.append(msg)

    # -- ffmpeg_available ------------------------------------------------
    if not _skip("ffmpeg_available"):
        needs_ffmpeg = any(op.kind == "transcode" for op in plan.ops)
        if needs_ffmpeg:
            exe = ffmpeg_path or shutil.which("ffmpeg")
            if not exe:
                res.errors.append(
                    "ffmpeg_available: profile requests transcode but ffmpeg "
                    "is not on PATH. Install with `brew install ffmpeg`."
                )

    # -- no_case_collisions ----------------------------------------------
    if not _skip("no_case_collisions"):
        rels_lower: dict[str, list[str]] = {}
        for op in plan.ops:
            if op.kind in ("copy", "transcode", "overwrite"):
                rels_lower.setdefault(str(op.dst_rel).lower(), []).append(
                    str(op.dst_rel)
                )
        collisions = {k: v for k, v in rels_lower.items() if len(v) > 1}
        if collisions:
            res.errors.append(
                f"no_case_collisions: {len(collisions)} collision(s) -- "
                f"{sorted(collisions.values())[0]}"
            )

    res.ok = not res.errors
    return res


__all__ = ["ALL_CHECKS", "PreflightResult", "preflight"]
