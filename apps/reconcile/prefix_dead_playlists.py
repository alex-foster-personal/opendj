"""Prefix Rekordbox playlists by file-availability health.

Mini-PRD (status: ✔︎ ✅ 🎯 done + working + regression tests):

R1. Classify every Rekordbox playlist by the share of its file-backed tracks
    that resolve on disk:
      * dead = <30% have
      * half = 30–80% have
      * healthy = >=80% have
    Streaming-only playlists, empty playlists, and folders with no
    file-backed descendants are left alone.
    [if a 100-track playlist has 25 working files then prefix == "[dead] " ⛔️]
    [if a 100-track playlist has 90 working files then prefix == "" ⛔️]

R2. Apply the prefix to the Rekordbox ``Name`` column for both leaf playlists
    and folders (folder health rolls up its descendants). Idempotent:
    re-running first strips any existing ``[dead]|[half]`` prefix before
    recomputing so health changes do not stack up.
    [if a name is already "[half] X" and now dead then result == "[dead] X" ⛔️]

R3. Safety: refuse to write if Rekordbox is running; take a timestamped
    backup of master.db; emit a reversal script that restores the backup.
    Dry-run by default — must pass ``--apply`` to mutate the live DB.
    [if pgrep is unavailable then the live write fails closed ⛔️]
    [if transactional readback differs then rollback occurs before commit ⛔️]
    [if every rename reads back then commit occurs exactly once ⛔️]

Live-write contract: never modifies any track row, only ``DjmdPlaylist.Name``.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pyrekordbox import Rekordbox6Database

from apps.shared import paths
from apps.shared import rekordbox_db as rb
from apps.shared.rekordbox_writeback import require_writeback_enabled

# Prefix grammar: "[dead] " or "[half] " (single trailing space, ASCII only).
DEAD_PREFIX = "[dead] "
HALF_PREFIX = "[half] "
_EXISTING_PREFIX_RE = re.compile(r"^\[(dead|half)\]\s+")

DEAD_THRESHOLD = 0.30  # have_ratio < this -> dead
HALF_THRESHOLD = 0.80  # have_ratio < this (and >=DEAD) -> half


class SafetyCheckError(RuntimeError):
    """Raised when the live-write preflight cannot prove Rekordbox is closed."""


@dataclass(slots=True)
class PlaylistHealth:
    pid: str
    name: str
    is_folder: bool
    have: int
    missing: int

    @property
    def file_total(self) -> int:
        return self.have + self.missing

    @property
    def have_ratio(self) -> float:
        return self.have / self.file_total if self.file_total else 1.0

    @property
    def desired_prefix(self) -> str:
        if self.file_total == 0:
            return ""  # leave streaming-only / empty as-is
        if self.have_ratio < DEAD_THRESHOLD:
            return DEAD_PREFIX
        if self.have_ratio < HALF_THRESHOLD:
            return HALF_PREFIX
        return ""

    @property
    def stripped_name(self) -> str:
        return _EXISTING_PREFIX_RE.sub("", self.name)

    @property
    def new_name(self) -> str:
        return self.desired_prefix + self.stripped_name


def _track_present(t: rb.RBTrack) -> bool:
    """True if the track is file-backed and resolves on disk."""
    if t.is_streaming or not t.folder_path:
        return False
    return os.path.exists(t.folder_path)


def compute_health(db) -> list[PlaylistHealth]:
    """Walk every playlist + roll folder health up from descendants."""
    tracks = {t.id: t for t in rb.iter_tracks(db)}
    raw: list[tuple[str, str, str | None, list[str]]] = []
    for p in db.get_playlist():
        pid = str(p.ID)
        parent = getattr(p, "ParentID", None)
        parent = None if parent in (None, "", "root", "0") else str(parent)
        songs = list(getattr(p, "Songs", []) or [])
        tids = [
            str(s.ContentID)
            for s in songs
            if getattr(s, "ContentID", None) is not None
        ]
        raw.append((pid, p.Name or "", parent, tids))

    kids: dict[str | None, list[str]] = defaultdict(list)
    for pid, _, parent, _ in raw:
        kids[parent].append(pid)
    by_id = {r[0]: r for r in raw}
    folders = {k for k in kids if k is not None}

    def leaf_counts(tids: list[str]) -> tuple[int, int]:
        h = m = 0
        for tid in tids:
            t = tracks.get(tid)
            if t is None or t.is_streaming or not t.folder_path:
                continue
            if os.path.exists(t.folder_path):
                h += 1
            else:
                m += 1
        return h, m

    out: dict[str, PlaylistHealth] = {}

    def visit(pid: str) -> tuple[int, int]:
        _, name, _, tids = by_id[pid]
        is_folder = pid in folders
        if is_folder:
            h = m = 0
            for cid in kids[pid]:
                ch, cm = visit(cid)
                h += ch
                m += cm
        else:
            h, m = leaf_counts(tids)
        out[pid] = PlaylistHealth(
            pid=pid, name=name, is_folder=is_folder, have=h, missing=m
        )
        return h, m

    for root_pid in kids[None]:
        visit(root_pid)
    return list(out.values())


def plan_renames(healths: list[PlaylistHealth]) -> list[PlaylistHealth]:
    """Return playlists whose name needs to change (idempotent)."""
    return [h for h in healths if h.new_name != h.name]


def _rekordbox_running() -> bool:
    """Mirror apps.reconcile.apply._rekordbox_running but exact-match.

    The shipped apply uses ``pgrep -if rekordbox`` which false-positives on
    grep pipes containing the substring (e.g. ``pyrekordbox``). We match the
    process name exactly instead.
    """
    try:
        r = subprocess.run(
            ["pgrep", "-x", "rekordbox"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise SafetyCheckError(
            "pgrep is unavailable; cannot prove Rekordbox is closed"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SafetyCheckError(
            "pgrep timed out; cannot prove Rekordbox is closed"
        ) from exc
    if r.returncode == 1:
        return False
    if r.returncode == 0 and r.stdout.strip():
        return True
    if r.returncode == 0:
        raise SafetyCheckError("pgrep reported a match without a process ID")
    detail = r.stderr.strip() or "no diagnostic output"
    raise SafetyCheckError(
        f"pgrep failed with exit {r.returncode}: {detail}"
    )


def _backup_db(backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    dst = backup_dir / f"master.{ts}.db"
    shutil.copy2(paths.REKORDBOX_LIVE_DB, dst)
    if dst.stat().st_size <= 0:
        raise RuntimeError(f"backup failed: {dst} is empty")
    return dst


def _write_reversal(
    plan: list[PlaylistHealth], backup: Path, backup_dir: Path
) -> Path:
    """Emit a Python script that restores every renamed playlist by ID."""
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    out = backup_dir / f"reverse-prefix-{ts}.py"
    pairs = ",\n    ".join(
        f"({p.pid!r}, {p.name!r})" for p in plan
    )
    body = f'''#!/usr/bin/env python
"""Reverse the [dead]/[half] playlist-rename run at {ts}.

Restores each playlist to its name from before the apply. The full DB backup
is at {backup} if you'd rather copy that over master.db instead.
"""
from pyrekordbox import Rekordbox6Database
from apps.shared import paths

PAIRS = [
    {pairs}
]

def main() -> None:
    db = Rekordbox6Database(path=str(paths.REKORDBOX_LIVE_DB))
    try:
        for pid, original in PAIRS:
            row = db.get_playlist(ID=pid)
            if row is None:
                print(f"  ! playlist {{pid}} not found; skipping")
                continue
            row.Name = original
        db.commit()
    finally:
        db.close()
    print(f"reverted {{len(PAIRS)}} playlists")

if __name__ == "__main__":
    main()
'''
    out.write_text(body, encoding="utf-8")
    out.chmod(0o755)
    return out


def _print_plan(plan: list[PlaylistHealth]) -> None:
    by_class: dict[str, list[PlaylistHealth]] = {"dead": [], "half": [], "clear": []}
    for p in plan:
        pref = p.desired_prefix
        if pref == DEAD_PREFIX:
            by_class["dead"].append(p)
        elif pref == HALF_PREFIX:
            by_class["half"].append(p)
        else:
            by_class["clear"].append(p)
    print(
        f"\nplan: {len(plan)} renames "
        f"(dead={len(by_class['dead'])}, "
        f"half={len(by_class['half'])}, "
        f"clear-existing-prefix={len(by_class['clear'])})\n"
    )
    for label, group in by_class.items():
        if not group:
            continue
        print(f"--- {label} ({len(group)}) ---")
        for p in group[:20]:
            kind = "📁" if p.is_folder else "  "
            print(
                f"  {kind} {p.name!r:50} -> {p.new_name!r:55} "
                f"(have={p.have}/{p.file_total}, "
                f"{100 * p.have_ratio:.0f}%)"
            )
        if len(group) > 20:
            print(f"  ... and {len(group) - 20} more")


def _apply(plan: list[PlaylistHealth]) -> int:
    require_writeback_enabled("module.reconcile.prefix_dead_playlists")
    """Write and verify every rename in one transaction before committing."""
    db = Rekordbox6Database(path=str(paths.REKORDBOX_LIVE_DB))
    try:
        for p in plan:
            row = db.get_playlist(ID=p.pid)
            if row is None:
                raise RuntimeError(
                    f"playlist {p.pid} ({p.name!r}) disappeared before apply"
                )
            row.Name = p.new_name

        db.session.flush()  # type: ignore[attr-defined]
        db.session.expire_all()  # type: ignore[attr-defined]
        mismatches: list[str] = []
        for p in plan:
            row = db.get_playlist(ID=p.pid)
            actual = None if row is None else (row.Name or "")
            if actual != p.new_name:
                mismatches.append(
                    f"{p.pid}: expected {p.new_name!r}, got {actual!r}"
                )
        if mismatches:
            raise RuntimeError(
                "playlist rename readback mismatch before commit: "
                + "; ".join(mismatches[:5])
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return len(plan)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m apps.reconcile.prefix_dead_playlists",
        description=__doc__,
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write to the live Rekordbox DB. Without this flag we dry-run.",
    )
    ap.add_argument(
        "--i-understand-the-risks",
        action="store_true",
        help="Required alongside --apply.",
    )
    ap.add_argument(
        "--backup-dir",
        type=Path,
        default=paths.DATA_DIR / "reconcile" / "backups",
    )
    args = ap.parse_args(argv)

    # Read from the working copy so the plan is reproducible.
    paths.copy_live_dbs()
    db = rb.open_db()
    try:
        healths = compute_health(db)
    finally:
        try:
            db.close()
        except Exception as exc:  # noqa: BLE001
            print(f"warning: failed to close working DB: {exc!r}")
    plan = plan_renames(healths)

    _print_plan(plan)

    if not args.apply:
        print("\n(dry-run) re-run with --apply --i-understand-the-risks to write.")
        return 0
    if not args.i_understand_the_risks:
        print("\nERROR: --apply requires --i-understand-the-risks.", file=sys.stderr)
        return 2
    if _rekordbox_running():
        print(
            "\nABORT: Rekordbox is running. Quit it (menu bar too) and retry.",
            file=sys.stderr,
        )
        return 3
    if not plan:
        print("nothing to rename. exiting clean.")
        return 0

    backup = _backup_db(args.backup_dir)
    print(f"\nbackup -> {backup}")
    rev = _write_reversal(plan, backup, args.backup_dir)
    print(f"reversal -> {rev}")

    wrote = _apply(plan)
    print(f"wrote {wrote}/{len(plan)} renames to live DB")
    print(f"verified readback before commit: {wrote}/{len(plan)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
