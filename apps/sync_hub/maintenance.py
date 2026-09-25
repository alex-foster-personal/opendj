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
import errno
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from apps.shared import engine_origin
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.shared.sync_runtime_gates import (
    DEFER_REASON_SYNC_IN_PROGRESS,
    SyncDeferredError,
    any_deck_playing,
    refuse_sync_round,
)
from apps.sync_hub import (
    capabilities,
    client,
    config_cli,
    engine,
    enrollment_credentials,
    fleet_admin,
    generation,
    hosted_config,
    maintenance_enroll,
    maintenance_policy,
    single_flight,
    sync_set,
)
from apps.sync_hub import status as sync_status
from apps.sync_hub.transport import SyncTransportError, classify_transport_failure

#: Exit code for a sync that COMPLETED without verifying agreement (round 5
#: gate T8). Distinct from 1: a caller must be able to tell "the merge is
#: wrong or the hub is down" from "rows moved but the check was excluded", and
#: distinct from 0 because an unmeasured comparison is not a clean one.
EXIT_INCONCLUSIVE: int = 4

#: Exit code when a sync round is deferred before any hub I/O (CLOUDSYNC-14).
EXIT_SYNC_DEFERRED: int = 3

#: Exit code for a sync whose PUSH a hosted hub refused on plan grounds while
#: its pull completed. Distinct from 1 (nothing came in) and from 4 (nothing
#: was withheld): here rows arrived and this machine's own edits did not leave.
EXIT_PUSH_REFUSED: int = 5


def _open(data_dir: Path) -> sqlite3.Connection:
    return state_db.open_rw(client.state_db_path(data_dir))


def _sync_error_message(exc: Exception) -> str:
    """Map a failed sync to an operator-facing journal line."""
    if isinstance(exc, SyncTransportError) and exc.code == "SYNC_PROTOCOL":
        text = str(exc)
        if capabilities.HASH_PENDING_V1 in text or "hash_pending" in text:
            return capabilities.hash_pending_upgrade_message()
    if isinstance(exc, SyncTransportError):
        classified = classify_transport_failure(str(exc))
        if classified is not None:
            return f"{classified.headline}: {exc}"
    if isinstance(exc, client.SyncDigestMismatch):
        return str(exc)
    return str(exc)


def sync(
    data_dir: Path,
    hub_url: str,
    *,
    name: str | None = None,
    transport: client.HubTransport | None = None,
    ui_mirror: Mapping[str, Any] | None = None,
    force: bool = False,
) -> client.SyncResult:
    """Run one spoke round trip against ``hub_url``. The spoke's operator entry.

    ``transport`` is here for the tests that drive the real router in-process;
    the CLI never passes it, so an operator always talks real HTTP. Any of
    ``run_sync``'s declared failures (see its docstring) propagate out
    unchanged -- fail fast, no repair.

    ``client.run_sync`` itself is wrapped in ``single_flight.sync_flock_for``,
    a cross-process ``fcntl.flock`` on a file in ``data_dir`` (claude-review /
    Codex, PR #3831, P1/BLOCKING): this function is the ONE place the
    scheduler, the ``POST /api/v1/cloudsync/sync`` route, and this CLI all
    funnel through to reach ``run_sync``, and the first two already hold an
    in-process lock (``single_flight.sync_lock_for``) before calling here, but
    that lock is invisible to the CLI's own separate process. ``force`` skips
    only the Gig-posture / playing-deck safety gate above; it never skips
    this -- two concurrent rounds corrupting the same ``state.db`` is a data
    integrity failure, not a safety judgement call an operator can override.
    A contended flock defers BEFORE any hub I/O, same as the gate above.
    """
    reason = refuse_sync_round(data_dir, ui_mirror, force=force)
    if reason is not None:
        raise SyncDeferredError(reason)
    try:
        with single_flight.sync_flock_for(data_dir):
            started_at = sync_stamp.canonical_now()
            try:
                result = client.run_sync(
                    Path(data_dir), hub_url, transport=transport, name=name
                )
            except Exception as exc:
                message = _sync_error_message(exc)
                sync_status.write_result(
                    Path(data_dir),
                    sync_status.SyncResult(
                        finished_at=sync_stamp.canonical_now(),
                        status="error",
                        message=message,
                        pushed=0,
                        pulled=0,
                    ),
                )
                raise
    except single_flight.SyncInProgressError as exc:
        raise SyncDeferredError(DEFER_REASON_SYNC_IN_PROGRESS) from exc
    sync_status.write_result(Path(data_dir), _journal_entry(result, started_at, data_dir))
    return result


def _journal_entry(
    result: client.SyncResult, started_at: str, data_dir: Path
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
        hub_held = "unreported" if result.hub_quarantined is None else result.hub_quarantined
        conn = _open(data_dir)
        try:
            exclusion_summary = sync_set.format_inconclusive_exclusion_summary(
                conn,
                result.quarantined_rows,
                hub_held,
                hub_quarantined=result.hub_quarantined,
            )
        finally:
            conn.close()
        return sync_status.SyncResult(
            finished_at=sync_stamp.canonical_now(),
            status="inconclusive",
            message=(
                f"sync started at {started_at} completed, but the digest "
                f"compare against hub {result.hub_machine_id} "
                f"{exclusion_summary} Agreement was not verified."
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
    sync_command.add_argument(
        "--force",
        action="store_true",
        help="Bypass Gig posture and playing-deck gates for this round only.",
    )

    subcommands.add_parser("generation", parents=[common], help="print this hub's generation token")
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
    prune_command.add_argument("--keep-days", type=float, default=engine.DEFAULT_KEEP_DAYS)
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

    feedback_command = subcommands.add_parser(
        "feedback-pins",
        help="sync or inspect a running engine's feedback pins (FBSYNC-04)",
    )
    feedback_command.add_argument("action", choices=("sync", "status"))
    feedback_command.add_argument(
        "--engine", required=True, help="the engine base URL, e.g. http://127.0.0.1:8728"
    )
    feedback_command.add_argument(
        "--pin-id", default=None, help="status only: narrow the answer to one pin"
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


def _report_sync(result: client.SyncResult, data_dir: Path) -> int:
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
        on_hub = "unreported" if result.hub_quarantined is None else result.hub_quarantined
        conn = _open(data_dir)
        try:
            remedy = sync_set.inconclusive_remedy(
                conn,
                held_here=result.quarantined_rows,
                hub_quarantined=result.hub_quarantined,
            )
        finally:
            conn.close()
        print(
            f"excluded: {result.quarantined_rows} row(s) held here, "
            f"{on_hub} on the hub, {result.quarantined_incoming} incoming "
            f"row(s) refused; {remedy}"
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
    minted = maintenance_enroll.grant(args.data_dir, owner_email=args.owner, ttl_s=args.ttl_seconds)
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


#: CFG. A feedback-pins sync is a whole CloudSync round trip on the engine.
FEEDBACK_PINS_CLI_TIMEOUT_S: float = 300.0


def _feedback_pins(args: argparse.Namespace) -> int:
    """FBSYNC-04 CLI twin: drive a RUNNING engine's feedback pin sync over HTTP.

    A thin shell, like ``enroll``: it opens no database and no comments.json,
    because the engine owns both and holds the lock every pin write takes.
    Prints the engine's JSON answer; exits 1 on any HTTP error or an
    unreachable engine, printing why.
    """
    base = args.engine.rstrip("/")
    if args.action == "sync":
        request = urllib.request.Request(f"{base}/api/v1/feedback/sync", data=b"", method="POST")
    else:
        query = f"?{urllib.parse.urlencode({'pin_id': args.pin_id})}" if args.pin_id else ""
        request = urllib.request.Request(f"{base}/api/v1/feedback/sync/status{query}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=FEEDBACK_PINS_CLI_TIMEOUT_S) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(f"[ERROR] feedback-pins {args.action}: HTTP {exc.code} {exc.read().decode()}")
        return 1
    except urllib.error.URLError as exc:
        print(f"[ERROR] feedback-pins {args.action}: engine {base} unreachable: {exc.reason}")
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


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
EXIT_CODE_COMMANDS: frozenset[str] = frozenset({"sync", "status", "feedback-pins", "policy"})


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


#: Timeout for the CLI's best-effort UI-mirror GET against a verified live
#: engine (the identity health check that precedes it has its OWN timeout,
#: ``apps.shared.engine_origin.IDENTITY_PROBE_TIMEOUT_S``, not this one).
#: Short on purpose: this runs on the ``sync`` critical path. The two probes
#: are sequential, not shared, so a wedged engine can hold the command for
#: roughly the SUM of both timeouts per lock checked, not just this value
#: once (claude-review, PR #3831, P3).
_LIVE_MIRROR_PROBE_TIMEOUT_S = 3.0


#: Reason code for `SyncDeferredError` when a verified live engine cannot be
#: read conclusively -- see `_cli_live_ui_mirror`.
DEFER_REASON_ENGINE_MIRROR_UNREACHABLE = "engine_mirror_unreachable"

#: Loopback spellings a lock file's ``host`` must be for a refused connection
#: on it to mean anything (see `_refused_by_loopback_engine`). The lock JSON
#: is not restricted to loopback by `engine_origin.resolve_origin`, so this
#: is checked explicitly rather than assumed.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: How stale a VERIFIED engine's ui-mirror document may be before the CLI
#: refuses to trust it (Codex review, PR #3831, P1/BLOCKING). The performance
#: page republishes every 1 s and the page's OWN stall detector
#: (``apps/webui/frontend/src/lib/rb/mirror-publish-stall.ts``,
#: ``MIRROR_STALL_MS``) still calls a gap this size healthy, so reusing that
#: exact threshold -- rather than inventing a second number -- means a
#: document this old is a page that has genuinely stopped publishing, not
#: ordinary jitter. A stale document cannot be trusted either way: if it
#: shows idle, the deck may have started playing since; if it shows playing,
#: `any_deck_playing` already reads that as playing regardless of age, which
#: is the safe direction anyway.
_UI_MIRROR_FRESHNESS_TOLERANCE_S = 5.0


def _candidate_lock_files(data_dir: Path) -> list[Path]:
    """The lock file(s) that might describe the engine playing a deck.

    Normally just ``<data_dir>/.engine.lock`` -- the lock naming the engine
    actually serving THIS data dir. Other CLIs in this repo instead resolve
    through ``apps.shared.engine_origin.lock_path``, which falls back to
    ``DEFAULT_LOCK_PATH`` (not a data-dir join) when
    ``OPENDJ_LIVE_LOCK_PATH`` is unset; this function honors that SAME
    override, layered on top of the data-dir lock rather than replacing it.
    When the override is set AND names a different file than the data-dir
    lock, BOTH are checked (claude-review, PR #3831, P2): the override lets
    an agent drive a sandboxed engine, but a DJ's own real engine at
    ``data_dir`` can be live at the same time, and its playing deck is
    exactly what this gate must not miss.
    """
    data_dir_lock = data_dir / ".engine.lock"
    override = os.environ.get(engine_origin.LOCK_PATH_ENV, "").strip()
    if not override:
        return [data_dir_lock]
    override_lock = Path(override).expanduser()
    # Resolved, not compared as-typed (claude-review, PR #3831, P3): a
    # relative --data-dir and an absolute override that name the SAME file
    # would otherwise be seen as different, probing (and blocking on) one
    # engine twice.
    if override_lock.resolve() == data_dir_lock.resolve():
        return [data_dir_lock]
    return [data_dir_lock, override_lock]


def _refused_by_loopback_engine(host: str, exc: BaseException | None) -> bool:
    """True only for a VERIFIED loopback ``ECONNREFUSED`` -- see the P1 note
    in `_probe_engine_lock`.

    ``httpx.ConnectError`` alone is not enough (Sol review, PR #3831,
    P1/BLOCKING): it also covers a DNS failure and an unreachable network,
    and the lock file's ``host`` is not restricted to loopback, so neither
    the exception type nor a non-loopback host refusing to connect proves
    "genuinely absent" -- it proves only that THIS attempt could not reach
    THAT host, which says nothing about whether an engine is alive
    elsewhere. Only a loopback host that itself actively refused the
    connection -- an OS-level ``ConnectionRefusedError`` (errno
    ``ECONNREFUSED``) -- means no process is listening on that port at all.

    The OS error is buried under one or two wrapper layers depending on
    whether it reached us via ``__cause__`` (this module's own ``except ...
    from exc`` chaining) or ``args[0]`` (how ``httpx``/``httpcore`` nest their
    own wrapped exceptions), so this walks both.
    """
    if host not in _LOOPBACK_HOSTS:
        return False
    current: BaseException | None = exc
    for _ in range(5):
        if current is None:
            return False
        if isinstance(current, ConnectionRefusedError):
            return True
        if isinstance(current, OSError) and current.errno == errno.ECONNREFUSED:
            return True
        if current.__cause__ is not None:
            current = current.__cause__
            continue
        args = current.args
        current = args[0] if args and isinstance(args[0], BaseException) else None
    return False


def _ui_mirror_received_at(body: Mapping[str, Any]) -> datetime | None:
    """Parse the server-stamped ``received_at`` (``apps/webui/server/routes/
    state.py``), or None when it is missing or not a valid ISO-8601 stamp."""
    raw = body.get("received_at")
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _ui_mirror_is_fresh(body: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    """True when a VERIFIED engine's 200 ui-mirror body is recent enough to
    trust (Codex review, PR #3831, P1/BLOCKING).

    ``GET /api/v1/state/ui-mirror`` serves the last document it received
    indefinitely; it is not re-stamped on read. A 200 is therefore only
    evidence the ROUTE answered, not that the document describes NOW -- if
    the page's main thread stalls after starting playback but before its
    next 1 s publish, the route keeps serving the last (idle) snapshot while
    the deck plays on. See `_UI_MIRROR_FRESHNESS_TOLERANCE_S`.
    """
    received_at = _ui_mirror_received_at(body)
    if received_at is None:
        return False
    current = now if now is not None else datetime.now(UTC)
    return (current - received_at).total_seconds() <= _UI_MIRROR_FRESHNESS_TOLERANCE_S


def _probe_engine_lock(lock_file: Path) -> Mapping[str, Any] | None:
    """The ``ui_mirror`` a single lock file's engine reports, or None if safe.

    A deck can only be playing while the engine that owns it is alive, so
    this verifies the process the lock names really is the identity-matched
    engine, then asks it for its current mirror over loopback HTTP.

    Only TWO outcomes read as "nothing is playing" here, both because no
    engine is reachable to own a playing deck: no lock file at this path at
    all, or a verified-origin lookup whose failure is a VERIFIED loopback
    ``ECONNREFUSED`` -- see `_refused_by_loopback_engine`. ``httpx.
    ConnectError`` alone is not enough: it also covers a DNS failure or an
    unreachable network, and the lock's ``host`` is not restricted to
    loopback, so those must defer, not pass (Sol review, PR #3831,
    P1/BLOCKING). Every OTHER ``EngineNotRunning`` -- an unusable or
    unreadable lock, a role/boot_id mismatch, a non-200 health status, or
    (critically) a ``httpx.TimeoutException`` from a port that accepted the
    connection and then hung on ``/api/v1/health`` -- is inconclusive, not
    permissive (claude-review, PR #3831, P1/BLOCKING): the lock names a
    specific engine, so its failure to check out is a reason to defer, not a
    reason to assume safety, and a wedged-but-locked engine is exactly the
    "wedged during a live set" case this gate exists to catch.

    A verified engine's own 409 with EXACTLY the documented
    ``{"client_open": False}`` body also reads as safe: no open performance
    page means no deck is rendering audio either. Everything else from a
    VERIFIED engine is inconclusive, not permissive: a non-200 status other
    than that exact 409 is a genuine server error, not a "not playing"
    signal; a transport failure on the mirror GET itself (unlike the identity
    check above) means an engine we just confirmed is alive stopped
    answering mid-probe; a response body that fails to parse as JSON at all
    must defer rather than raise an uncaught decode error out of a sync
    command; and a 200 body whose ``received_at`` is missing, invalid, or
    older than `_UI_MIRROR_FRESHNESS_TOLERANCE_S` is a stale snapshot that
    cannot be trusted to describe whether a deck is playing NOW (Codex
    review, PR #3831, P1/BLOCKING) -- see `_ui_mirror_is_fresh`. All of these
    raise `SyncDeferredError` so the CLI fails closed (refuses the sync)
    rather than silently assuming it is safe to proceed, or crashing instead
    of exiting with the documented deferred code.
    """
    try:
        origin = engine_origin.resolve_origin(lock_file)
    except engine_origin.EngineNotRunning as exc:
        if not lock_file.exists():
            return None
        raise SyncDeferredError(DEFER_REASON_ENGINE_MIRROR_UNREACHABLE) from exc
    try:
        engine_origin.verify_engine_identity(origin)
    except engine_origin.EngineNotRunning as exc:
        if _refused_by_loopback_engine(origin.host, exc.__cause__):
            return None
        raise SyncDeferredError(DEFER_REASON_ENGINE_MIRROR_UNREACHABLE) from exc
    try:
        with httpx.Client(timeout=_LIVE_MIRROR_PROBE_TIMEOUT_S) as http_client:
            response = http_client.get(f"{origin.base_url}/api/v1/state/ui-mirror")
    except httpx.TransportError as exc:
        raise SyncDeferredError(DEFER_REASON_ENGINE_MIRROR_UNREACHABLE) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise SyncDeferredError(DEFER_REASON_ENGINE_MIRROR_UNREACHABLE) from exc
    if response.status_code == 409:
        if body == {"client_open": False}:
            return None
        raise SyncDeferredError(DEFER_REASON_ENGINE_MIRROR_UNREACHABLE)
    if response.status_code != 200:
        raise SyncDeferredError(DEFER_REASON_ENGINE_MIRROR_UNREACHABLE)
    if not isinstance(body, dict):
        raise SyncDeferredError(DEFER_REASON_ENGINE_MIRROR_UNREACHABLE)
    if not _ui_mirror_is_fresh(body):
        raise SyncDeferredError(DEFER_REASON_ENGINE_MIRROR_UNREACHABLE)
    return body


def _cli_live_ui_mirror(data_dir: Path) -> Mapping[str, Any] | None:
    """Best-effort ``ui_mirror`` for a standalone CLI invocation (CLOUDSYNC-14).

    The CLI is its own process, so it never has the in-process ``ui_mirror``
    the running webui app keeps on ``Request.app.state`` -- that is only
    populated by an open performance page pushing to ``PUT
    /api/v1/state/ui-mirror`` inside THAT process. Every candidate lock file
    from ``_candidate_lock_files`` is probed via ``_probe_engine_lock``,
    which itself raises ``SyncDeferredError`` immediately for an inconclusive
    engine, so an inconclusive answer on ANY candidate defers the whole
    round. Among candidates that answer conclusively, a body showing a
    playing deck always wins over one that doesn't (claude-review, PR #3831,
    P2): otherwise a sandboxed engine named by ``OPENDJ_LIVE_LOCK_PATH`` that
    happens to report first, with nothing playing, would hide a REAL playing
    deck on the engine actually running against ``data_dir``.
    """
    first_body: Mapping[str, Any] | None = None
    for lock_file in _candidate_lock_files(data_dir):
        body = _probe_engine_lock(lock_file)
        if body is not None and any_deck_playing(body):
            return body
        if first_body is None:
            first_body = body
    return first_body


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
        try:
            return _report_sync(
                sync(
                    args.data_dir,
                    args.hub,
                    name=args.name,
                    ui_mirror=(None if args.force else _cli_live_ui_mirror(args.data_dir)),
                    force=args.force,
                ),
                args.data_dir,
            )
        except SyncDeferredError as exc:
            print(f"DEFERRED: {exc.reason}", file=sys.stderr)
            return EXIT_SYNC_DEFERRED
    if args.command == "status":
        return _report_status(sync_status.read_status(args.data_dir))
    if args.command == "feedback-pins":
        return _feedback_pins(args)
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
    "EXIT_SYNC_DEFERRED",
    "PRINTING_COMMANDS",
    "SyncDeferredError",
    "main",
    "prune",
    "rotate",
    "show_generation",
    "sync",
]
