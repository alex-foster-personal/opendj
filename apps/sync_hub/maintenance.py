"""Operator actions on a sync hub or spoke, as functions and as a CLI.

    uv run python -m apps.sync_hub sync         --data-dir DIR --hub URL [--name N]
    uv run python -m apps.sync_hub status       --data-dir DIR
    uv run python -m apps.sync_hub generation   --data-dir DIR
    uv run python -m apps.sync_hub rotate       --data-dir DIR
    uv run python -m apps.sync_hub prune        --data-dir DIR [--changelog T]
                                                [--keep-days N] [--keep-rows N]
    uv run python -m apps.sync_hub grant        --data-dir DIR --owner EMAIL
                                                [--ttl-seconds N]
    uv run python -m apps.sync_hub enroll       --data-dir DIR --hub URL
                                                (--grant TOKEN | --grant-file F)
                                                [--name N]
    uv run python -m apps.sync_hub fleet        --data-dir DIR [--json]
    uv run python -m apps.sync_hub policy <verb> --data-dir DIR ...  (see maintenance_policy)
    uv run python -m apps.sync_hub config show|set --data-dir DIR
                                                [--enabled|--disabled]
                                                [--hub URL] [--name N]
    uv run python -m apps.sync_hub adopt        --data-dir DIR --machine-id ID
                                                --owner EMAIL
    uv run python -m apps.sync_hub revoke       --data-dir DIR --machine-id ID
    uv run python -m apps.sync_hub credentials  --data-dir DIR [--json]
    uv run python -m apps.sync_hub hosted       --data-dir DIR

``config`` (see :mod:`apps.sync_hub.config_cli`) is the CLI twin of
``GET/PUT /api/v1/cloudsync/config``: the persisted per-machine config the
background scheduler (:mod:`apps.sync_hub.scheduler`) reads.

Eight operations, plus ``policy`` (per-machine sync policy, its own module
:mod:`apps.sync_hub.maintenance_policy`, dry-run by default, exit 0/1/3/4),
plus ``fleet`` (who owns which machine on this hub), plus adopt, revoke and
credentials, which live in :mod:`apps.sync_hub.fleet_admin` (plan X5) and
register themselves below, plus ``hosted`` (JSON: whether this hub is HOSTED,
its entitlement provider and owner count, the same checks the webui runs at
startup -- :mod:`apps.sync_hub.hosted_config`):

* **sync** runs one spoke round trip against ``--hub`` (round 3 finding R7).
  Nothing outside pytest called ``run_sync`` before -- the whole spoke
  protocol, and the retention prune it depends on, was library code with no
  operator or agent entry point. Its HTTP twin is ``POST
  /api/v1/cloudsync/sync`` (``apps/webui/server/routes/cloudsync_ops.py``),
  which calls :func:`sync` itself. The ``/cloudsync`` page has no sync button
  yet (plan W17).
* **status** prints the CloudSync status object, including the journal every
  :func:`sync` writes; its HTTP twin is ``GET /api/v1/cloudsync/status``.
* **prune** bounds a changelog that had no retention at all (round 2 finding
  N6) -- one row per synced-table write, forever. It only ever drops entries
  that a NEWER entry for the same row supersedes, so the pull loses no
  coverage and ``MAX(seq)`` cannot move.
* **rotate** re-mints this hub's generation token by hand, for the restore
  case the anchor cannot see on its own: a whole-machine restore rolls the
  data dir back too, so the anchor agrees with the DB and nothing looks
  wrong. Every spoke then re-offers its library once.
* **generation** prints the current token.
* **grant** mints one single-use enrollment credential on this hub, for a
  user who has signed in through the webui. Hub-local, because minting a
  credential is an act of hub authority. HTTP twin: ``POST
  /api/v1/cloudsync/enrollment-grants`` (loopback-only, hub-only).
* **enroll** is the DEV half of ``specs/design_decision_12.md``: the
  formalized single method for adding a machine to cloudsync. It is a thin
  shell over ``POST /api/v1/sync/enroll`` -- it opens no database and holds
  no enrollment logic of its own, so it cannot drift from the endpoint the
  in-app path will use. Idempotent: a re-run says "already enrolled".
* **fleet** prints who owns which machine on this hub, and how many
  machines are unowned. That count is what an operator closes before
  enrollment is ever enforced. HTTP twin: ``GET /api/v1/cloudsync/fleet``,
  which returns exactly ``fleet --json``.

Every UI/daemon action in this repo has a CLI twin (the agent-native parity
rule); these are the twin the sync surface will match.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.sync_hub import (
    client,
    config_cli,
    engine,
    enrollment_credentials,
    fleet_admin,
    generation,
    hosted_config,
    maintenance_enroll,
    maintenance_policy,
)
from apps.sync_hub import status as sync_status

#: Exit code for a sync that COMPLETED without verifying agreement (round 5
#: gate T8). Distinct from 1: a caller must be able to tell "the merge is
#: wrong or the hub is down" from "rows moved but the check was excluded", and
#: distinct from 0 because an unmeasured comparison is not a clean one.
EXIT_INCONCLUSIVE: int = 4

#: Exit code for a sync whose PUSH a hosted hub refused on plan grounds while
#: its pull completed. Distinct from 1 (nothing came in) and from 4 (nothing
#: was withheld): here rows arrived and this machine's own edits did not leave.
EXIT_PUSH_REFUSED: int = 5


def _open(data_dir: Path) -> sqlite3.Connection:
    return state_db.open_rw(client.state_db_path(data_dir))


def sync(
    data_dir: Path,
    hub_url: str,
    *,
    name: str | None = None,
    transport: client.HubTransport | None = None,
) -> client.SyncResult:
    """Run one spoke round trip against ``hub_url``. The spoke's operator entry.

    ``transport`` is here for the tests that drive the real router in-process;
    the CLI never passes it, so an operator always talks real HTTP. Any of
    ``run_sync``'s declared failures (see its docstring) propagate out
    unchanged -- fail fast, no repair.
    """
    started_at = sync_stamp.canonical_now()
    try:
        result = client.run_sync(
            Path(data_dir), hub_url, transport=transport, name=name
        )
    except Exception as exc:
        sync_status.write_result(
            Path(data_dir),
            sync_status.SyncResult(
                finished_at=sync_stamp.canonical_now(),
                status="error",
                message=str(exc),
                pushed=0,
                pulled=0,
            ),
        )
        raise
    sync_status.write_result(Path(data_dir), _journal_entry(result, started_at))
    return result


def _journal_entry(
    result: client.SyncResult, started_at: str
) -> sync_status.SyncResult:
    """The journal row one COMPLETED ``run_sync`` earns. Round 5 gate T8.

    A sync whose post-sync digest compare was inconclusive did not verify
    that the two sides agree -- one of them held rows out of the comparison,
    so the tables that differ differ for a reason nobody measured. It is not
    an error (rows moved, nothing raised) and it is not ``ok`` either, and
    stamping ``ok`` on it is exactly the "failed measurement rendered as a
    clean result" ``.claude/rules/verification.md`` exists to stop. So the
    journal carries the third verdict rather than rounding to one of two.

    A refused push is ``error``: rows came IN, but this machine's edits did
    not go out, so the sync did not do its job and ``ok`` would be a lie.
    """
    if result.push_refused:
        return sync_status.SyncResult(
            finished_at=sync_stamp.canonical_now(),
            status="error",
            message=(
                f"sync started at {started_at}: hub {result.hub_machine_id} "
                f"REFUSED the push (entitlement_not_in_plan: the owner's plan "
                f"is read_only). Pulled {result.pulled} row(s); this "
                f"machine's edits stay local and are offered again next sync."
            ),
            pushed=result.accepted,
            pulled=result.pulled,
        )
    if result.digest_inconclusive:
        return sync_status.SyncResult(
            finished_at=sync_stamp.canonical_now(),
            status="inconclusive",
            message=(
                f"sync started at {started_at} completed, but the digest "
                f"compare against hub {result.hub_machine_id} EXCLUDED rows "
                f"on at least one side ({result.quarantined_rows} held here, "
                f"{'unreported' if result.hub_quarantined is None else result.hub_quarantined}"
                f" on the hub), so agreement was not verified. Run `python -m "
                f"apps.shared.state.normalize_stamps --live` on the machine "
                f"holding the unorderable row, then sync again."
            ),
            pushed=result.pushed,
            pulled=result.pulled,
        )
    return sync_status.SyncResult(
        finished_at=sync_stamp.canonical_now(),
        status="ok",
        message=f"completed sync started at {started_at}",
        pushed=result.pushed,
        pulled=result.pulled,
    )


def prune(
    data_dir: Path,
    *,
    changelog: str = engine.HUB_CHANGELOG_TABLE,
    keep_days: float = engine.DEFAULT_KEEP_DAYS,
    keep_rows: int = engine.DEFAULT_KEEP_ROWS,
) -> int:
    """Prune one changelog in ``data_dir``'s state DB. Returns rows deleted."""
    conn = _open(data_dir)
    try:
        conn.execute("BEGIN")
        try:
            deleted = engine.prune_changelog(
                conn, changelog=changelog, keep_days=keep_days, keep_rows=keep_rows
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return deleted
    finally:
        conn.close()


def rotate(data_dir: Path) -> str:
    """Re-mint the hub generation token. Returns the new token."""
    conn = _open(data_dir)
    try:
        return generation.rotate(data_dir, seq=engine.current_seq(conn))
    finally:
        conn.close()


def show_generation(data_dir: Path) -> str:
    """This hub's current token, minting or rotating it as ``hello`` would."""
    conn = _open(data_dir)
    try:
        return generation.observe(conn, data_dir)
    finally:
        conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.sync_hub", description=__doc__.splitlines()[0]
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        required=True,
        type=Path,
        help="the data dir holding state/state.db and machine-id",
    )

    sync_command = subcommands.add_parser(
        "sync", parents=[common], help="run one spoke round trip against a hub"
    )
    sync_command.add_argument(
        "--hub", required=True, help="the hub base URL, e.g. http://hub.tailnet:8686"
    )

    subcommands.add_parser(
        "status", parents=[common], help="print the CloudSync status object as JSON"
    )
    sync_command.add_argument(
        "--name",
        default=None,
        help="this machine's display name; defaults to the hostname",
    )

    subcommands.add_parser(
        "generation", parents=[common], help="print this hub's generation token"
    )
    subcommands.add_parser(
        "rotate",
        parents=[common],
        help="re-mint the token; every spoke re-offers its library once",
    )
    prune_command = subcommands.add_parser(
        "prune", parents=[common], help="drop superseded changelog entries"
    )
    prune_command.add_argument(
        "--changelog",
        default=engine.HUB_CHANGELOG_TABLE,
        choices=sorted(engine.CHANGELOG_TABLES),
    )
    prune_command.add_argument(
        "--keep-days", type=float, default=engine.DEFAULT_KEEP_DAYS
    )
    prune_command.add_argument("--keep-rows", type=int, default=engine.DEFAULT_KEEP_ROWS)

    grant_command = subcommands.add_parser(
        "grant",
        parents=[common],
        help="mint one single-use enrollment grant on this hub",
    )
    grant_command.add_argument(
        "--owner",
        required=True,
        help="email of a user who has signed in on this hub; never created here",
    )
    grant_command.add_argument(
        "--ttl-seconds",
        type=int,
        default=enrollment_credentials.GRANT_TTL_S,
        help=(
            "how long the grant stays redeemable, in seconds; default "
            "%(default)s, and refused above "
            f"{enrollment_credentials.GRANT_TTL_MAX_S}"
        ),
    )

    enroll_command = subcommands.add_parser(
        "enroll",
        parents=[common],
        help="add THIS machine to a hub's fleet under a proved owner",
    )
    enroll_command.add_argument(
        "--hub", required=True, help="the hub base URL, e.g. http://hub.tailnet:8686"
    )
    # Exactly one grant source, enforced by argparse rather than by a runtime
    # check that could be reached with neither set.
    grant_source = enroll_command.add_mutually_exclusive_group(required=True)
    grant_source.add_argument(
        "--grant",
        default=None,
        help=(
            "a token from `python -m apps.sync_hub grant` on the hub. The "
            "other credential kind, google_id_token, is the in-app path: "
            "POST it to /api/v1/sync/enroll on a hub configured with the "
            "OAuth client id. Prefer --grant-file: an argument is readable "
            "in /proc and lands in shell history."
        ),
    )
    grant_source.add_argument(
        "--grant-file",
        type=Path,
        default=None,
        help="read the grant from this file, or from stdin when it is '-'",
    )
    enroll_command.add_argument(
        "--name",
        default=None,
        help=(
            "this machine's display name; defaults to the hostname. Pass it "
            "explicitly on WSL: machines.name is UNIQUE and a WSL hostname is "
            "often the Windows host's."
        ),
    )

    fleet_command = subcommands.add_parser(
        "fleet", parents=[common], help="print who owns which machine on this hub"
    )
    fleet_command.add_argument(
        "--json",
        action="store_true",
        help="print the same readout as JSON (agent parity with the UI)",
    )
    fleet_admin.add_subcommands(subcommands, common)
    maintenance_policy.add_policy_parser(subcommands, common)
    config_cli.add_config_parser(subcommands, common)
    subcommands.add_parser(
        "hosted",
        parents=[common],
        help="print whether this hub is hosted, its entitlement provider and owner count",
    )
    return parser


def _report_sync(result: client.SyncResult) -> int:
    """Print one sync's outcome and return its exit code.

    Split out of :func:`main` to keep that function's mccabe count under the
    quality-gate limit, which the third verdict pushed it over. No behavior
    lives here that did not live in the branch it came from.
    """
    restored = ", hub restore detected" if result.hub_restore_detected else ""
    print(
        f"synced against hub {result.hub_machine_id}: pushed "
        f"{result.pushed} (accepted {result.accepted}, rejected "
        f"{result.rejected}), pulled {result.pulled} (applied "
        f"{result.applied}), {result.rounds} round(s), hub seq "
        f"{result.hub_seq}{restored}"
    )
    if result.quarantined_rows or result.hub_quarantined:
        on_hub = (
            "unreported" if result.hub_quarantined is None else result.hub_quarantined
        )
        print(
            f"quarantined: {result.quarantined_rows} row(s) held here, "
            f"{on_hub} on the hub, {result.quarantined_incoming} incoming "
            f"row(s) refused; repair with `python -m apps.shared.state."
            f"normalize_stamps --live` on the machine holding them"
        )
    if result.push_refused:
        print(
            "PUSH REFUSED: the hub's plan check (entitlement_not_in_plan) "
            "does not admit writes for this machine's owner; pulled rows "
            "arrived, local edits did not leave this machine"
        )
        return EXIT_PUSH_REFUSED
    if not result.digest_inconclusive:
        return 0
    # NOT a silent 0. The sync completed and the comparison did not, so the
    # one thing this command exists to confirm -- that the two sides agree --
    # was not established.
    print(
        "INCONCLUSIVE: the post-sync digest compare excluded rows on at "
        "least one side, so agreement was NOT verified"
    )
    return EXIT_INCONCLUSIVE


def _report_status(current: sync_status.CloudSyncStatus) -> int:
    """Print the status object and return the exit code its verdict earns."""
    print(json.dumps(current.to_wire(), sort_keys=True))
    verdict = None if current.last_result is None else current.last_result["status"]
    if verdict == "error":
        return 1
    if verdict == "inconclusive":
        return EXIT_INCONCLUSIVE
    return 0


def _print_generation(args: argparse.Namespace) -> None:
    print(show_generation(args.data_dir))


def _print_rotate(args: argparse.Namespace) -> None:
    print(rotate(args.data_dir))


def _print_prune(args: argparse.Namespace) -> None:
    deleted = prune(
        args.data_dir,
        changelog=args.changelog,
        keep_days=args.keep_days,
        keep_rows=args.keep_rows,
    )
    print(f"pruned {deleted} superseded {args.changelog} entries")


def _print_grant(args: argparse.Namespace) -> None:
    minted = maintenance_enroll.grant(
        args.data_dir, owner_email=args.owner, ttl_s=args.ttl_seconds
    )
    for line in maintenance_enroll.grant_lines(minted):
        print(line)


def _print_enroll(args: argparse.Namespace) -> None:
    outcome = maintenance_enroll.enroll(
        args.data_dir,
        args.hub,
        credential_kind=enrollment_credentials.GRANT_KIND,
        credential_value=maintenance_enroll.read_grant_token(
            token=args.grant, token_file=args.grant_file
        ),
        name=args.name,
    )
    for line in maintenance_enroll.enroll_lines(outcome):
        print(line)


def _print_fleet(args: argparse.Namespace) -> None:
    payload = maintenance_enroll.fleet(args.data_dir)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for line in maintenance_enroll.fleet_lines(payload):
        print(line)


def _print_hosted(args: argparse.Namespace) -> None:
    print(json.dumps(hosted_config.describe_for_cli(os.environ, args.data_dir), sort_keys=True))


#: Subcommand name -> handler, for the commands that PRINT and exit 0.
#: ``sync`` and ``status`` are not here: they own their own exit codes and
#: returning one is the whole point of them.
#:
#: A table rather than a longer if/elif chain. Enrollment adds three more
#: commands, and a chain grows one branch of complexity per command without
#: doing anything more interesting. A table also makes "every registered
#: subcommand is dispatched" a property a test can check against the parser,
#: rather than a missing branch nobody notices until an operator gets an
#: AssertionError.
#: The subcommands that return a MEANINGFUL exit code rather than printing
#: and exiting 0. Named here, not in the test, so "every registered
#: subcommand is dispatched" can be re-derived from the module instead of
#: from a list a test author kept up to date by hand.
EXIT_CODE_COMMANDS: frozenset[str] = frozenset({"sync", "status", "policy"})


PRINTING_COMMANDS: dict[str, Callable[[argparse.Namespace], None]] = {
    "generation": _print_generation,
    "rotate": _print_rotate,
    "prune": _print_prune,
    "grant": _print_grant,
    "enroll": _print_enroll,
    "fleet": _print_fleet,
    "config": config_cli.run_config,
    **fleet_admin.PRINTING_COMMANDS,
    "hosted": _print_hosted,
}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """argparse exits 2 on a usage error; ``policy`` documents 1, so remap it there."""
    try:
        return _parser().parse_args(argv)
    except SystemExit as exc:
        tokens = sys.argv[1:] if argv is None else argv
        if exc.code == 2 and maintenance_policy.is_policy_argv(tokens):
            raise SystemExit(maintenance_policy.EXIT_USAGE) from exc
        raise


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand. Returns a process exit code."""
    args = _parse_args(argv)
    if args.command == "sync":
        return _report_sync(sync(args.data_dir, args.hub, name=args.name))
    if args.command == "status":
        return _report_status(sync_status.read_status(args.data_dir))
    if args.command == "policy":
        return maintenance_policy.run(args)
    # Everything below prints and exits 0; the two above own their own codes.
    handler = PRINTING_COMMANDS.get(args.command)
    if handler is None:
        raise AssertionError(
            f"subcommand {args.command!r} is registered on the parser but has "
            f"no handler in PRINTING_COMMANDS; the two must be kept in step."
        )
    handler(args)
    return 0


__all__ = [
    "EXIT_CODE_COMMANDS",
    "EXIT_INCONCLUSIVE",
    "PRINTING_COMMANDS",
    "main",
    "prune",
    "rotate",
    "show_generation",
    "sync",
]
