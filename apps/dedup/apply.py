"""``python -m apps.dedup.apply`` -- rewrite RB playlist links to canonical.

Dry-run path (default):

    python -m apps.dedup.apply

Reads ``duplicate_clusters`` + ``track_aliases`` from the dedup DB, joins
against the live Rekordbox content rows to find playlist entries that
still reference alias paths, and writes a reviewable plan CSV.

Cautious live path:

    python -m apps.dedup.apply --live --i-understand-the-risks \\
        --clusters 1,2,3

Applies the rewrite for the listed cluster ids only. Safety rails mirror
``apps/reconcile/apply.py`` exactly:

  1. Typed confirm (``yes i understand``).
  2. Refuse to run if Rekordbox is open (pgrep -if rekordbox + pyrekordbox PID).
  3. Timestamped backup of master.db before any write.
  4. Update ``FolderPath`` on each DjmdContent row via pyrekordbox.
  5. Post-write verify: re-open the DB, read each updated row, confirm
     it now points at the canonical path.
  6. Emit a stand-alone reversal script that restores the DB from backup.

**Never** writes audio files, never deletes audio, never touches djay in
this plan (djay rating + tag rewrites live in Plan 02 ``apps.tags.apply``).
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from apps.shared import paths
from apps.shared.rekordbox_writeback import require_writeback_enabled

from . import schema as dedup_schema

CONFIRMATION_PHRASE = "yes i understand"


@dataclass(slots=True)
class RewritePlanRow:
    cluster_id: int
    canonical_path: str
    alias_path: str
    rb_content_ids: list[str]
    playlist_names: list[str]


def _load_rewrite_plan(
    conn: sqlite3.Connection, *, cluster_ids: set[int] | None = None
) -> list[RewritePlanRow]:
    where = ""
    params: tuple = ()
    if cluster_ids:
        placeholder = ",".join("?" for _ in cluster_ids)
        where = f"WHERE c.cluster_id IN ({placeholder})"
        params = tuple(sorted(cluster_ids))
    rows = conn.execute(
        f"""
        SELECT c.cluster_id, c.canonical_path, a.alias_path
          FROM duplicate_clusters c
          JOIN track_aliases a ON a.cluster_id = c.cluster_id
          {where}
         ORDER BY c.cluster_id, a.alias_path
        """,
        params,
    ).fetchall()
    return [
        RewritePlanRow(
            cluster_id=cid,
            canonical_path=canon,
            alias_path=alias,
            rb_content_ids=[],
            playlist_names=[],
        )
        for cid, canon, alias in rows
    ]


def _enrich_with_rb(
    plan: list[RewritePlanRow], rb_db_path: Path
) -> list[RewritePlanRow]:
    if not rb_db_path.exists():
        return plan
    conn = sqlite3.connect(rb_db_path)
    try:
        for row in plan:
            ids = [
                str(r[0])
                for r in conn.execute(
                    "SELECT ID FROM djmdContent WHERE FolderPath = ?",
                    (row.alias_path,),
                ).fetchall()
            ]
            row.rb_content_ids = ids
            if ids:
                placeholders = ",".join("?" for _ in ids)
                try:
                    names = [
                        str(r[0])
                        for r in conn.execute(
                            f"""
                            SELECT DISTINCT p.Name FROM djmdPlaylist p
                              JOIN djmdSongPlaylist sp ON sp.PlaylistID = p.ID
                             WHERE sp.ContentID IN ({placeholders})
                            """,
                            ids,
                        ).fetchall()
                    ]
                except sqlite3.Error:
                    names = []
                row.playlist_names = names
    finally:
        conn.close()
    return plan


def _write_plan_csv(plan: list[RewritePlanRow], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "cluster_id",
                "canonical_path",
                "alias_path",
                "rb_content_ids",
                "playlist_names",
            ]
        )
        for row in plan:
            w.writerow(
                [
                    row.cluster_id,
                    row.canonical_path,
                    row.alias_path,
                    ";".join(row.rb_content_ids),
                    ";".join(row.playlist_names),
                ]
            )


def _write_plan_summary(plan: list[RewritePlanRow], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    total = len(plan)
    with_rb = sum(1 for r in plan if r.rb_content_ids)
    affected_pls = {p for r in plan for p in r.playlist_names}
    out.write_text(
        f"""# Dedup rewrite plan

* Rows: {total}
* With RB matches: {with_rb}
* Playlists affected: {len(affected_pls)}

See `rewrite-plan.csv` for the full list. No writes were performed.
""",
        encoding="utf-8",
    )


class PgrepUnavailable(RuntimeError):
    """P07-03: pgrep is missing; callers must fail safe."""


def _rekordbox_running() -> bool:
    # P07-03: match apps/sync/playlist_apply.py (P03-02) -- missing pgrep
    # must not silently be treated as "Rekordbox not running".
    try:
        res = subprocess.run(
            ["pgrep", "-if", "rekordbox"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PgrepUnavailable("pgrep not installed on PATH") from exc
    return bool(res.stdout.strip())


def _confirm() -> bool:
    try:
        reply = input(f"Type `{CONFIRMATION_PHRASE}` to proceed: ").strip().lower()
    except EOFError:
        return False
    return reply == CONFIRMATION_PHRASE


def _timestamped_backup(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = dst_dir / f"{src.stem}.{ts}{src.suffix}"
    shutil.copy2(src, dst)
    return dst


def _write_reversal_script(backup: Path, target: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"reverse-dedup-{ts}.sh"
    out.write_text(
        f"""#!/usr/bin/env bash
# Generated by apps.dedup.apply. Restores {target} from the pre-write backup.
set -euo pipefail
cp -v "{backup}" "{target}"
echo "[reverse-dedup] restored {target} from {backup}"
""",
        encoding="utf-8",
    )
    try:
        out.chmod(0o755)
    except OSError:
        pass
    return out


def _apply_rewrites_live(
    plan: list[RewritePlanRow],
    *,
    rb_db_path: Path,
) -> tuple[int, list[str]]:
    from pyrekordbox import Rekordbox6Database  # local import

    # Defence in depth: ``rb_db_path`` arrives as an argument, so a future
    # caller that reaches this helper without going through run_apply still
    # cannot write toward the real library.
    require_writeback_enabled("module.dedup.apply")

    errors: list[str] = []
    count = 0
    db = Rekordbox6Database(path=str(rb_db_path), unlock=False)
    try:
        for row in plan:
            for cid in row.rb_content_ids:
                try:
                    content = db.get_content(ID=cid)
                    if content is None:
                        errors.append(f"content_id={cid} missing")
                        continue
                    content.FolderPath = row.canonical_path
                    count += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"content_id={cid}: {exc}")
        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass
    return count, errors


def _verify_rewrites(
    plan: list[RewritePlanRow], rb_db_path: Path
) -> list[str]:
    bad: list[str] = []
    conn = sqlite3.connect(rb_db_path)
    try:
        for row in plan:
            for cid in row.rb_content_ids:
                got = conn.execute(
                    "SELECT FolderPath FROM djmdContent WHERE ID = ?",
                    (cid,),
                ).fetchone()
                if got is None or got[0] != row.canonical_path:
                    bad.append(
                        f"id={cid} expected={row.canonical_path!r} got={got!r}"
                    )
    finally:
        conn.close()
    return bad


@dataclass
class DedupApplyPaths:
    dedup_db_path: Path | None = None
    rb_db_path: Path | None = None
    plan_csv: Path | None = None
    plan_md: Path | None = None
    backup_dir: Path | None = None


def run_apply(
    *,
    apply_paths: DedupApplyPaths | None = None,
    backup_dir: Path | None = None,
    dedup_db_path: Path | None = None,
    rb_db_path: Path | None = None,
    plan_csv: Path | None = None,
    plan_md: Path | None = None,
    cluster_ids: set[int] | None = None,
    live: bool = False,
    confirm_fn=None,
    allow_rb_running: bool = False,
) -> dict:
    if live:
        # Rail 0, and it must fire FIRST. Every other rail below happens after
        # something has already opened ``rb_db``; against an encrypted
        # master.db that raises sqlite3.DatabaseError, which is a crash, not a
        # refusal. Refusing here means the one-way gate answers before any
        # handle exists, whatever ``--rb-db`` was aimed at.
        require_writeback_enabled("module.dedup.apply")
    cfg = apply_paths or DedupApplyPaths()
    if any(
        value is not None
        for value in (dedup_db_path, rb_db_path, plan_csv, plan_md, backup_dir)
    ):
        cfg = DedupApplyPaths(
            dedup_db_path=dedup_db_path or cfg.dedup_db_path,
            rb_db_path=rb_db_path or cfg.rb_db_path,
            plan_csv=plan_csv or cfg.plan_csv,
            plan_md=plan_md or cfg.plan_md,
            backup_dir=backup_dir or cfg.backup_dir,
        )
    dedup_db = cfg.dedup_db_path or paths.DEDUP_FALLBACK_DB
    rb_db = cfg.rb_db_path or paths.REKORDBOX_WORKING_DB
    plan_csv = cfg.plan_csv or paths.DEDUP_REWRITE_PLAN_CSV
    plan_md = cfg.plan_md or paths.DEDUP_REWRITE_SUMMARY_MD
    backup_dir = cfg.backup_dir or (paths.DEDUP_DIR / "backups")

    conn = dedup_schema.ensure_schema(dedup_db)
    try:
        plan = _load_rewrite_plan(conn, cluster_ids=cluster_ids)
    finally:
        conn.close()
    plan = _enrich_with_rb(plan, rb_db)

    _write_plan_csv(plan, plan_csv)
    _write_plan_summary(plan, plan_md)

    result: dict = {
        "plan_rows": len(plan),
        "with_rb": sum(1 for r in plan if r.rb_content_ids),
        "dry_run": not live,
        "applied": 0,
        "errors": [],
        "backup": None,
        "reversal": None,
        "verified": True,
    }

    if not live:
        return result

    # ``allow_rb_running`` exists solely as a test-only bypass for rail 2
    # (pgrep abort). Production callers must never pass True. We enforce
    # this by requiring a pytest context when the flag is set; this makes
    # accidental production misuse loud instead of a silent rail bypass.
    if allow_rb_running and not os.getenv("PYTEST_CURRENT_TEST"):
        raise RuntimeError(
            "allow_rb_running is a test-only bypass; refusing to run outside pytest."
        )

    if not allow_rb_running:
        try:
            rb_running = _rekordbox_running()
        except PgrepUnavailable as exc:
            # P07-03: fail-safe instead of fail-open on missing pgrep.
            result["errors"].append(
                f"pgrep unavailable ({exc}); refusing to write "
                "(override with allow_rb_running in test-only contexts)."
            )
            return result
        if rb_running:
            result["errors"].append("Rekordbox is running; refusing to write.")
            return result

    if confirm_fn is None:
        confirm_fn = _confirm
    if not confirm_fn():
        result["errors"].append("Confirmation not given; abort.")
        return result

    if not rb_db.exists():
        result["errors"].append(f"RB DB missing: {rb_db}")
        return result

    backup = _timestamped_backup(rb_db, backup_dir)
    result["backup"] = str(backup)

    # v1.0 adversarial review (#3, HIGH): the reversal script must be
    # emitted BEFORE the live write, not after. If the rewrite crashes
    # mid-commit (SIGKILL, power loss, pyrekordbox exception during
    # commit), the operator needs a one-liner to restore master.db from
    # the timestamped backup we just took. Previously the script was
    # written only AFTER verify, so a crash mid-write produced a backup
    # with no recovery path.
    reversal = _write_reversal_script(backup, rb_db, backup_dir)
    result["reversal"] = str(reversal)

    count, errors = _apply_rewrites_live(plan, rb_db_path=rb_db)
    result["applied"] = count
    result["errors"].extend(errors)

    bad = _verify_rewrites(plan, rb_db)
    if bad:
        result["verified"] = False
        result["errors"].extend(bad)
        shutil.copy2(backup, rb_db)

    return result


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.dedup.apply",
        description="Rewrite RB playlist links from alias to canonical.",
    )
    p.add_argument("--db", type=Path, default=None, help="Dedup state DB.")
    p.add_argument("--rb-db", type=Path, default=None, help="Rekordbox DB path.")
    p.add_argument("--plan-csv", type=Path, default=None)
    p.add_argument("--plan-md", type=Path, default=None)
    p.add_argument(
        "--clusters",
        type=str,
        default=None,
        help="Comma-separated cluster ids. Omit to include all clusters.",
    )
    p.add_argument("--live", action="store_true")
    p.add_argument("--i-understand-the-risks", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cluster_ids: set[int] | None = None
    if args.clusters:
        cluster_ids = {int(x) for x in args.clusters.split(",") if x.strip()}

    if args.live and not args.i_understand_the_risks:
        print(
            "ERROR: --live requires --i-understand-the-risks.", file=sys.stderr
        )
        return 2

    res = run_apply(
        apply_paths=DedupApplyPaths(
            dedup_db_path=args.db,
            rb_db_path=args.rb_db,
            plan_csv=args.plan_csv,
            plan_md=args.plan_md,
        ),
        cluster_ids=cluster_ids,
        live=args.live,
    )
    print(
        f"dry_run={res['dry_run']} plan_rows={res['plan_rows']} "
        f"with_rb={res['with_rb']} applied={res['applied']} "
        f"verified={res['verified']} errors={len(res['errors'])}"
    )
    for err in res["errors"]:
        print(f"  error: {err}")
    return 0 if res["verified"] and not res["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
