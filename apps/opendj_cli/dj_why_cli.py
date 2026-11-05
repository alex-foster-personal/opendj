"""``opendj dj why`` and ``opendj dj verdict``: why AutoPlay chose a track, and how it sounded (ADR-0139).

    opendj dj why [--pair OUT:IN] [--json]
    opendj dj verdict good|meh|bad --out X --in Y

``why`` prints the stored decision in plain words: the shortlist, each rejection and its reason, every candidate's
style and rule score, the chosen candidate's warnings, and the verdict once one is given. ``--json`` prints the
document ``GET /api/v1/dj/transitions/last`` answers. Pure logging: neither call changes what the planner does.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlencode

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE, EXIT_UNCONFIRMED
from apps.opendj_cli.dj_pair_cli import DjPairService
from apps.opendj_cli.dj_service import DjError
from apps.opendj_cli.origin import EngineIdentityMismatch, EngineNotRunning, resolve_verified_origin

RECORD_PATH = "/api/v1/dj/transitions"
_REFUSED_CODES = frozenset({"decision_not_found", "pair_transition_not_found", "pair_invalid"})


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _why_parser() -> _Parser:
    parser = _Parser(prog="opendj dj why", description="why AutoPlay chose its last track, in plain words")
    parser.add_argument("--pair", help="OUT:IN stable ids; default the newest decision")
    parser.add_argument("--json", action="store_true", help="print the stored record as JSON")
    return parser


def _verdict_parser() -> _Parser:
    parser = _Parser(prog="opendj dj verdict", description="one-tap verdict on the transition that ran")
    parser.add_argument("verdict", choices=("good", "meh", "bad"))
    parser.add_argument("--out", dest="out_id", required=True, help="outgoing track stable_id")
    parser.add_argument("--in", dest="in_id", required=True, help="incoming track stable_id")
    return parser


def explain(record: Mapping[str, Any]) -> str:
    """The stored record in plain words, one fact per line."""
    decision = record["decision"]
    lines = [f"Decided {record['decided_at']} leaving {record['out_track_id']}: {decision['reason']}"]
    shortlist = decision.get("shortlist") or []
    lines.append(f"Shortlist ({len(shortlist)}): {', '.join(shortlist) if shortlist else 'not recorded'}")
    recent = decision.get("recent_styles") or []
    lines.append(
        f"Intent: {decision.get('intent') or 'default (smooth)'}; recent styles: {', '.join(recent) if recent else 'none'}"
    )
    lines.extend(f"  rejected {item['track']}: {item['reason']}" for item in decision.get("rejected") or [])
    chosen = decision.get("chosen")
    for candidate in decision.get("candidates") or []:
        mark = "CHOSEN" if candidate["cid"] == chosen else "offered"
        lines.append(
            f"  {mark} {candidate['track']} {candidate['style']} over {candidate['bars']} bars, "
            f"rule score {candidate['score']:.3f}"
        )
        if candidate["cid"] == chosen:
            warnings = candidate.get("warnings") or []
            lines.extend(f"    warning: {text}" for text in warnings)
            if not warnings:
                lines.append("    no validator warnings")
    played = record.get("played")
    lines.append(
        "Played: not yet" if played is None else f"Played {played['times_played']} time(s) as {played['style']}"
    )
    verdict = record.get("verdict")
    lines.append("Verdict: none given" if verdict is None else f"Verdict: {verdict} ({record['verdict_at']})")
    return "\n".join(lines) + "\n"


def _calls(rest: Sequence[str]) -> tuple[Callable[[DjPairService], dict[str, Any]], bool]:
    """The HTTP call for ``rest`` and whether its answer prints in plain words."""
    if rest and rest[0] == "verdict":
        args = _verdict_parser().parse_args(list(rest[1:]))
        body = {"out": args.out_id, "in": args.in_id, "verdict": args.verdict}
        return (lambda service: service.send("POST", f"{RECORD_PATH}/verdict", body=body)), False
    args = _why_parser().parse_args(list(rest[1:]))
    query = "" if args.pair is None else f"?{urlencode({'pair': args.pair})}"
    return (lambda service: service.send("GET", f"{RECORD_PATH}/last{query}")), not args.json


def run(
    rest: Sequence[str],
    *,
    lock: Path | None,
    service_factory: Callable[[Path | None], DjPairService] | None = None,
) -> int:
    """Run ``opendj dj why`` or ``opendj dj verdict`` (``rest`` starts with the tool name)."""
    try:
        call, plain = _calls(rest)
    except _UsageError as error:
        print(json.dumps({"error": "usage", "message": str(error)}, indent=2, sort_keys=True))
        return EXIT_FAILED
    try:
        service = (
            service_factory(lock)
            if service_factory is not None
            else DjPairService(origin=resolve_verified_origin(lock))
        )
        document = call(service)
    except (EngineIdentityMismatch, EngineNotRunning) as error:
        print(json.dumps({"error": "engine_not_running", "message": str(error)}, indent=2, sort_keys=True))
        return EXIT_NO_ENGINE
    except DjError as error:
        print(json.dumps(error.document(), indent=2, sort_keys=True))
        return EXIT_UNCONFIRMED if error.code in _REFUSED_CODES else EXIT_FAILED
    sys.stdout.write(explain(document) if plain else json.dumps(document, indent=2, sort_keys=True) + "\n")
    return EXIT_CONFIRMED
