"""``opendj dj try``: the transition try-er on the command line (TRYER-11).

    opendj dj try --out X --in Y [-n 6] [--context-bars 4] [--json]
    opendj dj try pick JOB CANDIDATE --etag ETAG [--json]
    opendj dj try show JOB [--json]
    opendj dj try undo JOB --etag ETAG [--json]
    opendj dj try lanes JOB CANDIDATE [--columns 800] [--json]
    opendj dj try save-wav JOB CANDIDATE --out NEW.wav [--json]

Read ``--etag`` from ``opendj dj try show JOB``.

Output is always the JSON job document ``POST /api/v1/dj/try`` returns (the
``--json`` flag is accepted for symmetry with the other surfaces and changes
nothing), so the CLI, the MCP tools and the try-er panel read one shape.
``lanes`` prints the lane document of ``GET .../candidates/{cid}/lanes`` (the
one ``TryerLanes.svelte`` draws); the route is the only ``--columns`` bound check.
``save-wav`` writes the bytes of ``GET .../candidates/{cid}/audio`` (the WAV the
panel auditions) to a NEW path: an existing target is refused before any request
(``output_exists``, exit 4) and is never overwritten, and a render the server has
already pruned answers the route's 404 ``render_missing`` (exit 1), writing nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE, EXIT_UNCONFIRMED
from apps.opendj_cli.dj_service import DjError
from apps.opendj_cli.dj_try_service import DjTryService
from apps.opendj_cli.origin import EngineIdentityMismatch, EngineNotRunning, resolve_verified_origin

#: Refusals of a well-formed request (stale etag, nothing to undo), exit 4 like ``opendj dj``'s refused orders.
#: A try that cannot run (try_failed, track_unavailable) is a failure, exit 1, as every other dj failure.
#: ``output_exists`` is the same kind: ``save-wav`` never overwrites a file.
_REFUSED_CODES = frozenset({"etag_mismatch", "no_pick", "already_picked", "if_match_required", "output_exists"})
_ACTIONS = ("pick", "show", "undo", "lanes", "save-wav")
_JSON_HELP = "output is always JSON; accepted for symmetry"
#: pick and undo are guarded writes: the route refuses a missing (428) or stale (412) etag, so say where to read it.
_ETAG_HELP = (
    "required: the etag of the job document you reviewed; read the current one from "
    "`opendj dj try show JOB` (every try, pick and undo answer also carries it)"
)


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _parser(action: str | None) -> _Parser:
    if action == "pick":
        parser = _Parser(prog="opendj dj try pick", description="record the chosen candidate for a try job")
        parser.add_argument("job_id")
        parser.add_argument("candidate_id")
        parser.add_argument("--etag", required=True, help=_ETAG_HELP)
        parser.add_argument("--json", action="store_true", help=_JSON_HELP)
        return parser
    if action == "show":
        parser = _Parser(prog="opendj dj try show", description="read a try job, its candidates and its pick")
        parser.add_argument("job_id")
        parser.add_argument("--json", action="store_true", help=_JSON_HELP)
        return parser
    if action == "undo":
        parser = _Parser(prog="opendj dj try undo", description="remove a try job's pick")
        parser.add_argument("job_id")
        parser.add_argument("--etag", required=True, help=_ETAG_HELP)
        parser.add_argument("--json", action="store_true", help=_JSON_HELP)
        return parser
    if action == "lanes":
        parser = _Parser(prog="opendj dj try lanes", description="read a kept candidate's waveforms and knob lines")
        parser.add_argument("job_id")
        parser.add_argument("candidate_id")
        parser.add_argument("--columns", type=int, default=800, help="waveform peaks per deck, 1..4000 (default 800)")
        parser.add_argument("--json", action="store_true", help=_JSON_HELP)
        return parser
    if action == "save-wav":
        parser = _Parser(
            prog="opendj dj try save-wav", description="save a kept candidate's rendered WAV to a new file"
        )
        parser.add_argument("job_id")
        parser.add_argument("candidate_id")
        parser.add_argument(
            "--out", dest="out_path", type=Path, required=True, help="a NEW .wav path; never overwritten"
        )
        parser.add_argument("--json", action="store_true", help=_JSON_HELP)
        return parser
    parser = _Parser(prog="opendj dj try", description="render, score and rank candidate transitions for a pair")
    parser.add_argument("--out", dest="out_id", required=True, help="outgoing track stable_id")
    parser.add_argument("--in", dest="in_id", required=True, help="incoming track stable_id")
    parser.add_argument("-n", type=int, default=6, help="candidates to render (default 6)")
    parser.add_argument("--context-bars", type=int, default=4, help="bars of context scored each side (default 4)")
    parser.add_argument("--json", action="store_true", help=_JSON_HELP)
    return parser


def _call(rest: Sequence[str]) -> Callable[[DjTryService], dict[str, Any]]:
    action = rest[0] if rest and rest[0] in _ACTIONS else None
    args = _parser(action).parse_args(list(rest[1:] if action else rest))
    if action == "pick":
        return lambda service: service.pick(args.job_id, args.candidate_id, args.etag)
    if action == "show":
        return lambda service: service.get_job(args.job_id)
    if action == "undo":
        return lambda service: service.undo(args.job_id, args.etag)
    if action == "lanes":
        return lambda service: service.lanes(args.job_id, args.candidate_id, args.columns)
    if action == "save-wav":
        _refuse_existing(args.out_path)
        return lambda service: _save_wav(service, args.job_id, args.candidate_id, args.out_path)
    return lambda service: service.try_transitions(args.out_id, args.in_id, args.n, args.context_bars)


def _refuse_existing(out_path: Path) -> None:
    """Refuse a target that already exists (a dangling symlink included), before any request is made."""
    if out_path.is_symlink() or out_path.exists():
        raise DjError("output_exists", f"{out_path} already exists; save-wav never overwrites, name a new path")


def _save_wav(service: DjTryService, job_id: str, candidate_id: str, out_path: Path) -> dict[str, Any]:
    """Download the candidate's audition WAV, then create ``out_path`` exclusively and write exactly those bytes."""
    audio = service.candidate_audio(job_id, candidate_id)
    try:
        with out_path.open("xb") as handle:
            handle.write(audio)
    except FileExistsError as error:  # created by someone else during the download
        raise DjError("output_exists", f"{out_path} appeared during the download; nothing was overwritten") from error
    except OSError as error:
        raise DjError("output_unwritable", f"cannot create {out_path}: {error.strerror}") from error
    return {
        "job_id": job_id,
        "candidate": candidate_id,
        "path": str(out_path.resolve()),
        "bytes": len(audio),
        "sha256": hashlib.sha256(audio).hexdigest(),
    }


def _emit(document: dict[str, Any]) -> None:
    print(json.dumps(document, indent=2, sort_keys=True))


def _dj_error_exit(error: DjError) -> int:
    _emit(error.document())
    return EXIT_UNCONFIRMED if error.code in _REFUSED_CODES else EXIT_FAILED


def run(
    rest: Sequence[str],
    *,
    lock: Path | None,
    service_factory: Callable[[Path | None], DjTryService] | None = None,
) -> int:
    """Run one ``opendj dj try`` call; ``service_factory`` is the tests' seam."""
    try:
        call = _call(rest)
    except _UsageError as error:
        _emit({"error": "usage", "message": str(error)})
        return EXIT_FAILED
    except DjError as error:  # a local refusal (save-wav's existing target), before any engine call
        return _dj_error_exit(error)
    try:
        service = (
            service_factory(lock) if service_factory is not None else DjTryService(origin=resolve_verified_origin(lock))
        )
        document = call(service)
    except EngineIdentityMismatch as error:
        _emit({"error": "engine_identity_mismatch", "message": str(error)})
        return EXIT_NO_ENGINE
    except EngineNotRunning as error:
        _emit({"error": "engine_not_running", "message": str(error)})
        return EXIT_NO_ENGINE
    except DjError as error:
        return _dj_error_exit(error)
    _emit(document)
    return EXIT_CONFIRMED
