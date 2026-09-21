#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Pull Modal App logs into a durable local archive before they expire.

WHY THIS EXISTS. Modal retains App logs for roughly a day, then they are
gone -- no export, no history API, nothing. Every GPU run this repo does
(scripts/modal_vocal_farm.py, modal_demucs_ab.py, modal_roformer_spike.py,
modal_vocal_spike.py, modal_vocal_ladder.py) leaves its full stdout/stderr
(errors, per-container timing, anything that never made it into a
scripts/farm_progress.py run_end event or a scripts/bench/*.json ledger)
sitting only in Modal's own log store. This script copies that store to
disk before the clock runs out, so a bench run from yesterday is still
debuggable today.

DISCOVERY, NOT A HARDCODED LIST. ``modal app list --json`` returns every
App Modal still has a record of (running or recently stopped), each with
the ``description`` the script gave modal.App(name=...). Apps in this repo
all share the ``mdt-`` prefix by convention (checked against every
scripts/modal_*.py at the time this was written), so filtering on that
prefix is filtering on "this repo's Modal apps" without maintaining a
second, driftable list of app names here.

ONE FILE PER (APP NAME, CALENDAR DAY), APPEND-ONLY. A bench session fires
many short-lived ephemeral Apps (one modal.App per ``modal run``), so
grouping by description+day keeps the archive from fragmenting into one
tiny file per run while still being easy to grep by date.

IDEMPOTENT BY DESIGN so this is safe to add to a cron/loop later, not just
run once by hand: each appended block is headed by its unique app_id, and
a block is skipped if that app_id's header is already present in the
target file. Re-running never duplicates a day's log.

Mini-PRD
--------
Status key: `→` out of scope | `?` todo | `✔︎` done | `✔︎ ✅` done + ran +
works as expected.

  ✔︎ ✅ enumerate this repo's Modal apps by name prefix, not a fixed list
    [if] a new scripts/modal_whatever.py app appears with the mdt- prefix
         [then] the next run picks it up with no code change
    [if] --app-prefix is overridden [then] only matching descriptions pull
    [if] `modal app list` itself fails [then ⛔️] raise with modal's stderr,
         never silently return zero apps

  ✔︎ ✅ pull full logs per app into data/state/modal-logs/<app>/<date>.log
    [if] an app has already been pulled (app_id header present) [then] skip
         it without re-fetching or duplicating lines
    [if] `modal app logs <id>` fails for one app [then ⛔️] raise immediately;
         already-written files for earlier apps in the run stay on disk
    [if] an app produced no log lines [then] its header block still records
         the attempt, so "pulled and empty" is distinguishable from
         "never pulled"

  ✔︎ ✅ --dry-run lists what would be pulled without calling `modal app
    logs` or touching the filesystem, for a cheap parity/smoke check
    [if] --dry-run is passed [then] no file under data/state/modal-logs/
         is created or modified

Run (repo already has the `modal` CLI authenticated as this Mac's profile):
    uv run scripts/pull_modal_logs.py
    uv run scripts/pull_modal_logs.py --dry-run
    uv run scripts/pull_modal_logs.py --app-prefix mdt-roformer

-Claude
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR: Path = REPO_ROOT / "data"
DEFAULT_APP_PREFIX: str = "mdt-"
DEFAULT_TAIL: int = 20_000  # modal's own `app logs --tail` ceiling
BLOCK_HEADER_PREFIX: str = "##### modal-logs-pull"


@dataclass(frozen=True)
class ModalApp:
    """One row from ``modal app list --json``."""

    app_id: str
    description: str
    state: str
    created_at: str
    stopped_at: str

    @property
    def created_date(self) -> str:
        """YYYY-MM-DD the archive file is bucketed by, from created_at."""
        # modal emits "2026-07-31 06:13:16+01:00"; the date is the first token.
        return self.created_at.split(" ", 1)[0]


# ---------------------------------------------------------------------------
# modal CLI calls
# ---------------------------------------------------------------------------


def _require_modal_cli() -> None:
    if shutil.which("modal") is None:
        raise SystemExit(
            "error: `modal` CLI not found on PATH. Install/auth it first "
            "(pyproject extra or `uv tool install modal` + `modal token set`)."
        )


def list_apps(app_prefix: str) -> list[ModalApp]:
    result = subprocess.run(
        ["modal", "app", "list", "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`modal app list --json` failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    rows = json.loads(result.stdout)
    apps = [
        ModalApp(
            app_id=row["app_id"],
            description=row.get("description") or "",
            state=row.get("state") or "unknown",
            created_at=row.get("created_at") or "",
            stopped_at=row.get("stopped_at") or "",
        )
        for row in rows
    ]
    return [app for app in apps if app.description.startswith(app_prefix)]


def fetch_app_logs(app_id: str, *, tail: int) -> str:
    result = subprocess.run(
        [
            "modal", "app", "logs", app_id,
            "--timestamps",
            "--show-function-id",
            "--show-container-id",
            "-n", str(tail),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`modal app logs {app_id}` failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


def archive_path(data_dir: Path, app: ModalApp) -> Path:
    return data_dir / "state" / "modal-logs" / app.description / f"{app.created_date}.log"


def already_pulled(path: Path, app_id: str) -> bool:
    if not path.is_file():
        return False
    marker = f"app_id={app_id} "
    return marker in path.read_text(encoding="utf-8")


def append_block(path: Path, app: ModalApp, log_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pulled_at = datetime.now(UTC).isoformat(timespec="seconds")
    header = (
        f"{BLOCK_HEADER_PREFIX} app_id={app.app_id} description={app.description} "
        f"state={app.state} created_at={app.created_at} stopped_at={app.stopped_at} "
        f"pulled_at={pulled_at} #####\n"
    )
    body = log_text if log_text.strip() else "(no log lines returned)\n"
    if not body.endswith("\n"):
        body += "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(header)
        fh.write(body)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--app-prefix", default=DEFAULT_APP_PREFIX,
        help=f"only pull Modal Apps whose description starts with this (default {DEFAULT_APP_PREFIX!r})",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
        help="repo data dir; logs land under <data-dir>/state/modal-logs/",
    )
    parser.add_argument(
        "--tail", type=int, default=DEFAULT_TAIL,
        help=f"max log lines to request per app (default {DEFAULT_TAIL})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="list apps that would be pulled; touch nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _require_modal_cli()

    apps = list_apps(args.app_prefix)
    if not apps:
        print(f"no Modal apps found matching prefix {args.app_prefix!r}")
        return 0

    pulled, skipped = 0, 0
    for app in apps:
        target = archive_path(args.data_dir, app)
        if already_pulled(target, app.app_id):
            print(f"skip  {app.app_id}  {app.description}  (already in {target})")
            skipped += 1
            continue
        if args.dry_run:
            print(f"would-pull  {app.app_id}  {app.description}  -> {target}")
            continue
        log_text = fetch_app_logs(app.app_id, tail=args.tail)
        append_block(target, app, log_text)
        line_count = len([ln for ln in log_text.splitlines() if ln.strip()])
        print(f"pulled {app.app_id}  {app.description}  {line_count} lines -> {target}")
        pulled += 1

    if args.dry_run:
        print(f"dry-run: {len(apps)} app(s) matched prefix {args.app_prefix!r}")
    else:
        print(f"done: {pulled} pulled, {skipped} already archived, {len(apps)} total matched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
