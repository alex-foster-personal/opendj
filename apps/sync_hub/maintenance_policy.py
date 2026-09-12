"""``python -m apps.sync_hub policy <verb>``: per-machine sync policy for agents.

    policy show     --data-dir DIR [--machine ID|self]
    policy classes  --data-dir DIR
    policy validate --data-dir DIR [--proposal FILE|-]
    policy plan     --data-dir DIR  --proposal FILE|-
    policy apply    --data-dir DIR  --proposal FILE|-                [--live]
    policy set      --data-dir DIR  --machine M --kind K --mode MODE [--budget MB] [--live]
    policy unset    --data-dir DIR  --machine M --kind K             [--live]
    policy pin      --data-dir DIR  --machine M --playlist P --mode MODE [--live]
    policy unpin    --data-dir DIR  --machine M --playlist P         [--live]
    policy seed     --data-dir DIR  --machine M --class CLASS        [--live]

The agent-native twin of ``/api/v1/cloudsync/policies/*``. Every verb that
changes anything builds a proposal and hands it to
:func:`apps.sync_hub.policy_store.apply_proposal`, the same function the HTTP
routes call, and is a DRY RUN unless ``--live`` is passed. ``--machine self``
names this machine. Output is always JSON on stdout.

Exit codes:

* 0 ok (for ``validate``: the resulting fleet breaks no error rule)
* 1 usage, or refused input (unknown machine, playlist or cell; bad proposal,
  including one that names a cell twice)
* 3 validation errors: ``validate`` found an error rule broken; ``plan``,
  ``apply`` and the edit verbs found a BLOCKING one. Nothing was written.
* 4 inconclusive: no machines are registered, so no rule could measure. Every
  verb that judges or writes returns it, and nothing is written.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from pydantic import ValidationError

from apps.cloud.policy import MACHINE_CLASSES
from apps.shared.state import db as state_db
from apps.sync_hub import client
from apps.sync_hub.data_classes import registry_payload
from apps.sync_hub.data_classes_types import POLICY_MODES
from apps.sync_hub.policy_rules import PinCell, PinKey, PolicyCell, PolicyKey, ProposedPolicy
from apps.sync_hub.policy_store import (
    PolicyInputError,
    apply_proposal,
    author_machine_id,
    empty_proposal,
    evaluate,
    list_live,
    seed_proposal,
)
from apps.sync_hub.policy_wire import PolicyChangesIn

EXIT_OK: int = 0
EXIT_USAGE: int = 1
EXIT_VIOLATIONS: int = 3
#: Same value as :data:`apps.sync_hub.maintenance.EXIT_INCONCLUSIVE` (pinned by a test).
EXIT_INCONCLUSIVE: int = 4

READ_VERBS: frozenset[str] = frozenset({"show", "classes", "validate", "plan"})
SELF: str = "self"


# ----- parser ----------------------------------------------------------------


def add_policy_parser(
    subcommands: argparse._SubParsersAction, common: argparse.ArgumentParser
) -> None:
    """Register ``policy`` and its verbs on the ``python -m apps.sync_hub`` parser."""
    policy = subcommands.add_parser("policy", help="per-machine sync policy (dry-run default)")
    verbs = policy.add_subparsers(dest="verb", required=True)
    show = verbs.add_parser("show", parents=[common], help="print live policy cells and pins")
    show.add_argument("--machine", default=None, help="only this machine (an id or 'self')")
    verbs.add_parser("classes", parents=[common], help="print the data-class registry")
    validate = verbs.add_parser("validate", parents=[common], help="judge the fleet; never writes")
    validate.add_argument(
        "--proposal", type=str, default=None, help="JSON file or -; omit to judge the stored fleet"
    )
    plan = verbs.add_parser("plan", parents=[common], help="rows a proposal would touch; no write")
    plan.add_argument("--proposal", type=str, required=True, help="JSON file, or - for stdin")
    apply = verbs.add_parser("apply", parents=[common], help="apply a proposal file")
    apply.add_argument("--proposal", type=str, required=True, help="JSON file, or - for stdin")
    edits = {
        "set": "set one policy cell",
        "unset": "tombstone one policy cell",
        "pin": "pin one playlist",
        "unpin": "tombstone one playlist pin",
        "seed": "one cell per asset kind from the machine-class defaults",
    }
    for name, help_text in edits.items():
        edit = verbs.add_parser(name, parents=[common], help=help_text)
        edit.add_argument("--machine", required=True, help="a machine id, or 'self'")
        _add_edit_arguments(name, edit)
    for name in ("apply", *edits):
        verbs.choices[name].add_argument(
            "--live", action="store_true", help="write; without it this is a dry run"
        )


def _add_edit_arguments(verb: str, parser: argparse.ArgumentParser) -> None:
    if verb in ("set", "unset"):
        parser.add_argument("--kind", required=True, help="an asset kind")
    if verb in ("pin", "unpin"):
        parser.add_argument("--playlist", required=True, help="a playlist id")
    if verb in ("set", "pin"):
        parser.add_argument("--mode", required=True, choices=POLICY_MODES)
    if verb == "set":
        parser.add_argument("--budget", type=int, default=None, help="MB; only for cached")
    if verb == "seed":
        parser.add_argument("--class", dest="machine_class", required=True, choices=MACHINE_CLASSES)


def is_policy_argv(argv: list[str]) -> bool:
    """True when ``argv`` invokes ``policy``, so a usage error exits 1, not 2."""
    positional = [token for token in argv if not token.startswith("-")]
    return bool(positional) and positional[0] == "policy"


# ----- proposal builders -----------------------------------------------------


def _load_changes(source: str) -> PolicyChangesIn:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    try:
        return PolicyChangesIn.model_validate_json(text)
    except ValidationError as exc:
        raise PolicyInputError("PROPOSAL_INVALID", 422, f"proposal {source}: {exc}") from exc


def _target(machine: str, author: str) -> str:
    return author if machine == SELF else machine


_EDIT_BUILDERS: dict[str, Callable[[argparse.Namespace, str, str], ProposedPolicy]] = {
    "set": lambda a, author, m: replace(
        empty_proposal(author), policies=(PolicyCell(m, a.kind, a.mode, a.budget),)
    ),
    "unset": lambda a, author, m: replace(
        empty_proposal(author), removed_policies=(PolicyKey(m, a.kind),)
    ),
    "pin": lambda a, author, m: replace(
        empty_proposal(author), pins=(PinCell(m, a.playlist, a.mode),)
    ),
    "unpin": lambda a, author, m: replace(
        empty_proposal(author), removed_pins=(PinKey(m, a.playlist),)
    ),
    "seed": lambda a, author, m: seed_proposal(author, m, a.machine_class),
}


def _edit_proposal(args: argparse.Namespace, author: str) -> ProposedPolicy:
    if args.verb == "apply":
        return _load_changes(args.proposal).to_proposed(author)
    return _EDIT_BUILDERS[args.verb](args, author, _target(args.machine, author))


# ----- dispatch --------------------------------------------------------------


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _read_verb(conn: sqlite3.Connection, args: argparse.Namespace, author: str) -> int:
    if args.verb == "show":
        machine = None if args.machine is None else _target(args.machine, author)
        _print(list_live(conn, machine))
        return EXIT_OK
    changes = PolicyChangesIn() if args.proposal is None else _load_changes(args.proposal)
    outcome = evaluate(conn, changes.to_proposed(author))
    _print(outcome.to_wire())
    if not outcome.measurable:
        return EXIT_INCONCLUSIVE
    failed = outcome.has_errors() if args.verb == "validate" else outcome.blocking
    return EXIT_VIOLATIONS if failed else EXIT_OK


def run(args: argparse.Namespace) -> int:
    """Run one ``policy`` verb against ``args.data_dir``. Returns the exit code."""
    if args.verb == "classes":
        _print(registry_payload())
        return EXIT_OK
    db_path = client.state_db_path(args.data_dir)
    if not db_path.exists():
        print(f"[ERROR] no state DB at {db_path}", file=sys.stderr)
        return EXIT_USAGE
    read_only = args.verb in READ_VERBS
    conn = state_db.open_ro(db_path) if read_only else state_db.open_rw(db_path)
    try:
        author = author_machine_id(conn)
        if read_only:
            return _read_verb(conn, args, author)
        outcome = apply_proposal(conn, _edit_proposal(args, author), live=args.live)
    except PolicyInputError as exc:
        _print({"code": exc.code, "message": str(exc)})
        return EXIT_USAGE
    finally:
        conn.close()
    _print(outcome.to_wire())
    if not outcome.measurable:
        return EXIT_INCONCLUSIVE
    return EXIT_VIOLATIONS if outcome.blocking else EXIT_OK


__all__ = [
    "EXIT_INCONCLUSIVE",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_VIOLATIONS",
    "add_policy_parser",
    "is_policy_argv",
    "run",
]
