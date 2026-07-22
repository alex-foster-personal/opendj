"""Cautious tag-unification writer for audio files.

Per-file flow:

1. Pre-write check: file exists; not an iCloud ``.icloud`` placeholder;
   RB + djay not running (pgrep-based).
2. Hash the file; copy to
   ``data/tags/backups/YYYY-MM-DD/<sha256>.ext`` (never overwrite).
3. Compute the UnifiedPlan via collect -> unify.
4. Write the unified tags via ``apps.shared.tag_writer.write_tags``.
5. Re-read the file, diff against the plan. On mismatch, restore from
   backup and fail the batch.
6. Append rows to ``tag_provenance`` (one per field) in the dedup DB.
7. Emit a per-batch reversal script that restores each file from its
   sha256-keyed backup.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import shutil
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from apps.shared import paths
from apps.shared.tag_writer import read_tags, write_tags

from apps.dedup import schema as dedup_schema
from . import collect as tag_collect
from . import unify as tag_unify


@dataclass(slots=True)
class ApplyResult:
    path: Path
    applied: dict
    backup: Path | None
    error: str | None = None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _icloud_placeholder(path: Path) -> bool:
    """True if the file is an iCloud not-yet-downloaded placeholder."""
    sibling = path.with_name("." + path.name + ".icloud")
    return sibling.exists()


def _in_pytest() -> bool:
    """True when running under pytest (used to gate test-only flags)."""
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


class PgrepUnavailable(RuntimeError):
    """P07-03: pgrep is missing or errored; caller must decide fail-safe."""


def _is_app_running(name: str) -> bool:
    # P07-03: refuse to silently fail open when pgrep is unavailable.
    # Callers translate the exception into an explicit guard result so the
    # blast radius matches apps/sync/playlist_apply.py (P03-02).
    try:
        r = subprocess.run(
            ["pgrep", "-if", name], capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise PgrepUnavailable("pgrep not installed on PATH") from exc
    return bool(r.stdout.strip())


def _backup(path: Path, root: Path) -> Path:
    day = _dt.datetime.now().strftime("%Y-%m-%d")
    dst_dir = root / day
    dst_dir.mkdir(parents=True, exist_ok=True)
    sha = _sha256(path)
    dst = dst_dir / f"{sha}{path.suffix}"
    if not dst.exists():
        shutil.copy2(path, dst)
    return dst


def _insert_provenance(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    plan: tag_unify.UnifiedPlan,
) -> None:
    rows = [
        (
            stable_id,
            field,
            "" if getattr(plan.tags, field) is None else str(getattr(plan.tags, field)),
            prov.source,
            prov.confidence,
            prov.modified_at,
        )
        for field, prov in plan.provenance.items()
    ]
    conn.executemany(
        "INSERT INTO tag_provenance "
        "(stable_id, field, value, source, confidence, modified_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def apply_one(
    path: Path,
    *,
    backup_root: Path,
    dedup_conn: sqlite3.Connection | None = None,
    stable_id: str | None = None,
    allow_app_running: bool = False,
    dry_run: bool = False,
    fetch_rb=None,
    fetch_djay=None,
    fetch_mik=None,
) -> ApplyResult:
    """Apply unified tags to a single file.

    Returns :class:`ApplyResult`; on error sets ``error`` and leaves the
    file unchanged (restored from backup if the write failed post-copy).
    """
    if not path.exists():
        return ApplyResult(path=path, applied={}, backup=None, error="missing file")
    if _icloud_placeholder(path):
        return ApplyResult(
            path=path, applied={}, backup=None, error="iCloud placeholder"
        )
    if allow_app_running and not _in_pytest():
        raise RuntimeError(
            "allow_app_running=True is a test-only escape hatch and must not be "
            "used outside pytest; refusing to bypass the running-app safety check."
        )
    if not allow_app_running:
        try:
            if _is_app_running("rekordbox"):
                return ApplyResult(
                    path=path, applied={}, backup=None, error="rekordbox running"
                )
            if _is_app_running("djay"):
                return ApplyResult(
                    path=path, applied={}, backup=None, error="djay running"
                )
        except PgrepUnavailable as exc:
            # P07-03: pgrep missing -> fail-safe (block the write) instead
            # of silently treating it as "no app running".
            return ApplyResult(
                path=path, applied={}, backup=None, error=f"pgrep unavailable: {exc}"
            )

    sources = tag_collect.collect_for(
        path, fetch_rb=fetch_rb, fetch_djay=fetch_djay, fetch_mik=fetch_mik
    )
    plan = tag_unify.unify(sources)

    if dry_run:
        return ApplyResult(path=path, applied={}, backup=None)

    backup = _backup(path, backup_root)
    try:
        result = write_tags(path, plan.tags, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        # restore from backup just in case
        if backup.exists():
            shutil.copy2(backup, path)
        return ApplyResult(
            path=path, applied={}, backup=backup, error=f"write failed: {exc}"
        )

    # Post-write verify: re-read and compare.
    got = read_tags(path)
    mismatches = []
    for field in plan.provenance:
        want = getattr(plan.tags, field)
        have = getattr(got, field)
        if want is None:
            continue
        if have != want:
            mismatches.append(f"{field}: got {have!r} want {want!r}")
    if mismatches:
        shutil.copy2(backup, path)
        return ApplyResult(
            path=path,
            applied={},
            backup=backup,
            error="verify failed: " + "; ".join(mismatches),
        )

    if dedup_conn is not None and stable_id is not None:
        # P07-02: the audio write has succeeded + been verified by this
        # point. A provenance-insert failure used to propagate and abort
        # the batch without rolling the file back, which was silently
        # confusing: the file on disk is correct, but the dedup DB has no
        # record of the change. Catch and surface as a non-fatal warning
        # on the result so the caller can log it and continue.
        try:
            _insert_provenance(dedup_conn, stable_id=stable_id, plan=plan)
        except sqlite3.DatabaseError as exc:
            return ApplyResult(
                path=path,
                applied=result.applied,
                backup=backup,
                error=f"provenance log failed (audio write OK): {exc}",
            )

    return ApplyResult(path=path, applied=result.applied, backup=backup)


def _write_reversal(apply_results: list[ApplyResult], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"reverse-tags-{ts}.sh"
    lines = ["#!/usr/bin/env bash", "set -euo pipefail"]
    for r in apply_results:
        if r.backup and r.error is None:
            # as_posix(): the reversal is a bash script; backslashed Windows
            # paths would be mangled by quoting/escaping rules.
            lines.append(f'cp -v "{r.backup.as_posix()}" "{Path(r.path).as_posix()}"')
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        out.chmod(0o755)
    except OSError:
        pass
    return out


def run_apply(
    *,
    files: list[Path],
    dedup_db_path: Path | None = None,
    stable_id_lookup: dict[str, str] | None = None,
    backup_root: Path | None = None,
    reversal_dir: Path | None = None,
    allow_app_running: bool = False,
    dry_run: bool = False,
    fetch_rb=None,
    fetch_djay=None,
    fetch_mik=None,
) -> dict:
    backup_root = backup_root or paths.TAGS_BACKUPS_DIR
    reversal_dir = reversal_dir or paths.TAGS_REVERSAL_DIR
    dedup_db = dedup_db_path or paths.DEDUP_FALLBACK_DB

    conn = dedup_schema.ensure_schema(dedup_db)
    try:
        results: list[ApplyResult] = []
        for path in files:
            sid = (stable_id_lookup or {}).get(str(path))
            res = apply_one(
                path,
                backup_root=backup_root,
                dedup_conn=conn if not dry_run else None,
                stable_id=sid,
                allow_app_running=allow_app_running,
                dry_run=dry_run,
                fetch_rb=fetch_rb,
                fetch_djay=fetch_djay,
                fetch_mik=fetch_mik,
            )
            results.append(res)
    finally:
        conn.close()

    # Only emit a reversal script when there is at least one successful
    # backup to reverse. An empty script (just the shebang) would be a
    # confusing artefact when every file errored before backup.
    has_reversible = any(r.backup and r.error is None for r in results)
    reversal = (
        _write_reversal(results, reversal_dir)
        if not dry_run and has_reversible
        else None
    )

    return {
        "results": results,
        "applied_count": sum(1 for r in results if r.applied and not r.error),
        "errors": [r.error for r in results if r.error],
        "reversal": str(reversal) if reversal else None,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m apps.tags.apply")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--live", action="store_true")
    p.add_argument("--i-understand-the-risks", action="store_true")
    p.add_argument("--backup-root", type=Path, default=None)
    p.add_argument("--reversal-dir", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.live and not args.i_understand_the_risks:
        print(
            "ERROR: --live requires --i-understand-the-risks.", file=sys.stderr
        )
        return 2
    res = run_apply(
        files=args.paths,
        backup_root=args.backup_root,
        reversal_dir=args.reversal_dir,
        dry_run=not args.live,
    )
    print(
        f"dry_run={not args.live} applied={res['applied_count']} "
        f"errors={len(res['errors'])} reversal={res['reversal']}"
    )
    for err in res["errors"]:
        print(f"  error: {err}")
    return 0 if not res["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
