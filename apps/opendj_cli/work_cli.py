"""``opendj work <verb>``: works, version stacks and classical metadata.

The CLI twin of every Works UI action (Library Organization PRD s7). Each verb
is one HTTP call to the running engine through :mod:`apps.opendj_cli.api_cli`,
so validation, typed errors and exit codes are the server's own and a verb can
never print success the server did not confirm.

Writes need an undo stack: ``--undo-session NAME``, default ``opendj-cli``.
``opendj work undo`` reverts the newest write on that stack.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from apps.opendj_cli import EXIT_FAILED, api_cli

DEFAULT_UNDO_SESSION = "opendj-cli"
_ROLES = ("original", "recording", "remix", "cover", "edit", "live", "mashup")
_API = "/api/v1"
_CLASSICAL_TEXT = ("composer", "catalogue", "movement", "soloist", "ensemble", "conductor", "label")
_WORK_TEXT = ("title", "composer", "catalogue", "movement")


@dataclass(frozen=True)
class WorkRequest:
    """One HTTP call a verb turns into."""

    method: str
    path: str
    body: Mapping[str, Any] | None = None

    @property
    def is_write(self) -> bool:
        return self.method != "GET"


def _segment(value: str) -> str:
    return quote(value, safe="")


def _sent(args: argparse.Namespace, names: Sequence[str]) -> dict[str, Any]:
    """Only the options the caller gave, so an edit never clears a field by omission."""
    return {name: getattr(args, name) for name in names if getattr(args, name) is not None}


def _create(args: argparse.Namespace) -> WorkRequest:
    body = {"kind": args.kind, "aliases": args.alias or [], **_sent(args, _WORK_TEXT)}
    return WorkRequest("POST", f"{_API}/works", body)


def _edit(args: argparse.Namespace) -> WorkRequest:
    body = _sent(args, _WORK_TEXT)
    if args.alias is not None:
        body["aliases"] = args.alias
    return WorkRequest("PATCH", f"{_API}/works/{_segment(args.work)}", body)


def _link_many(args: argparse.Namespace) -> WorkRequest:
    body = {"member_ids": args.tracks, "kind": args.kind, "role": args.role}
    if args.work is not None:
        body["work_id"] = args.work
    if args.title is not None:
        body["title"] = args.title
    return WorkRequest("POST", f"{_API}/works:link", body)


def _link(args: argparse.Namespace) -> WorkRequest:
    body = {"member_kind": "track", "member_id": args.track, "role": args.role,
            "is_primary": args.primary}
    return WorkRequest("POST", f"{_API}/works/{_segment(args.work)}/members", body)


def _member_path(args: argparse.Namespace) -> str:
    return f"{_API}/works/{_segment(args.work)}/members/track/{_segment(args.track)}"


def _unlink(args: argparse.Namespace) -> WorkRequest:
    return WorkRequest("DELETE", _member_path(args))


def _role(args: argparse.Namespace) -> WorkRequest:
    return WorkRequest("PATCH", _member_path(args), {"role": args.role})


def _primary(args: argparse.Namespace) -> WorkRequest:
    return WorkRequest("PATCH", _member_path(args), {"is_primary": True})


def _move(args: argparse.Namespace) -> WorkRequest:
    path = f"{_API}/works/{_segment(args.work)}/members:move"
    return WorkRequest("POST", path, {"member_ids": args.tracks})


def _merge(args: argparse.Namespace) -> WorkRequest:
    return WorkRequest("POST", f"{_API}/works/{_segment(args.work)}:merge", {"into": args.into})


def _top(args: argparse.Namespace) -> WorkRequest:
    path = f"{_API}/works/{_segment(args.work)}/pref"
    return WorkRequest("PUT", path, {"top_member_id": args.track})


def _dismiss(args: argparse.Namespace) -> WorkRequest:
    body = {"a_member": args.a, "b_member": args.b}
    return WorkRequest("POST", f"{_API}/works/dismissals", body)


def _stack(args: argparse.Namespace) -> WorkRequest:
    query = {"member": args.track}
    if args.playlist is not None:
        query["playlist_id"] = args.playlist
    return WorkRequest("GET", f"{_API}/works/stack?{urlencode(query)}")


def _suggest(args: argparse.Namespace) -> WorkRequest:
    return WorkRequest("GET", f"{_API}/works/suggest?{urlencode({'member': args.track})}")


def _candidates(args: argparse.Namespace) -> WorkRequest:
    query = urlencode({"member": args.track, "q": args.query})
    return WorkRequest("GET", f"{_API}/works/link-candidates?{query}")


def _classical(args: argparse.Namespace) -> WorkRequest:
    path = f"{_API}/tracks/{_segment(args.track)}/classical"
    body = _sent(args, (*_CLASSICAL_TEXT, "recording_year"))
    return WorkRequest("PATCH", path, body) if body else WorkRequest("GET", path)


def _file_tags(args: argparse.Namespace) -> WorkRequest:
    return WorkRequest("GET", f"{_API}/tracks/{_segment(args.track)}/classical/file-tags")


def _simple(method: str, template: str, body: Mapping[str, Any] | None = None) -> Any:
    def build(args: argparse.Namespace) -> WorkRequest:
        path = template.format(**{k: _segment(str(v)) for k, v in vars(args).items()})
        return WorkRequest(method, f"{_API}{path}", body)
    return build


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opendj work", exit_on_error=False)
    parser.add_argument("--undo-session", default=DEFAULT_UNDO_SESSION)
    verbs = parser.add_subparsers(dest="verb", required=True)

    def verb(name: str, build: Any, *positional: str) -> argparse.ArgumentParser:
        sub = verbs.add_parser(name, exit_on_error=False)
        sub.set_defaults(build=build)
        for item in positional:
            sub.add_argument(item)
        return sub

    def work_fields(sub: argparse.ArgumentParser) -> None:
        for name in _WORK_TEXT:
            sub.add_argument(f"--{name}")
        sub.add_argument("--alias", action="append")

    verb("list", _simple("GET", "/works"))
    verb("overlay", _simple("GET", "/works/overlay"))
    create = verb("create", _create)
    work_fields(create)
    create.add_argument("--kind", choices=("classical", "song"), default="song")
    work_fields(verb("edit", _edit, "work"))
    verb("delete", _simple("DELETE", "/works/{work}"), "work")
    many = verb("link-many", _link_many)
    many.add_argument("tracks", nargs="+")
    many.add_argument("--work")
    many.add_argument("--title")
    many.add_argument("--kind", choices=("classical", "song"), default="song")
    many.add_argument("--role", choices=_ROLES, default="remix")
    link = verb("link", _link, "work", "track")
    link.add_argument("--role", choices=_ROLES, default="remix")
    link.add_argument("--primary", action="store_true")
    verb("unlink", _unlink, "work", "track")
    role = verb("role", _role, "work", "track")
    role.add_argument("role", choices=_ROLES)
    verb("primary", _primary, "work", "track")
    verb("move", _move, "work").add_argument("tracks", nargs="+")
    verb("merge", _merge, "work", "into")
    verb("top", _top, "work", "track")
    verb("dismiss", _dismiss, "a", "b")
    verb("stack", _stack, "track").add_argument("--playlist")
    verb("suggest", _suggest, "track")
    verb("candidates", _candidates, "track").add_argument("--query", default="")
    classical = verb("classical", _classical, "track")
    for name in _CLASSICAL_TEXT:
        classical.add_argument(f"--{name}")
    classical.add_argument("--recording-year", type=int)
    verb("file-tags", _file_tags, "track")
    verb("undo", _simple("POST", "/undo"))
    return parser


def build_request(rest: Sequence[str]) -> tuple[WorkRequest, str]:
    """Turn argv into the HTTP call it means, plus the undo stack it writes to."""
    args = _parser().parse_args(list(rest))
    return args.build(args), args.undo_session


def run(
    rest: Sequence[str],
    *,
    as_json: bool = False,
    environ: Mapping[str, str] | None = None,
    lock: Path | None = None,
) -> int:
    try:
        request, undo_session = build_request(rest)
    except (argparse.ArgumentError, SystemExit) as error:
        print(f"[ERROR] opendj work: {error}", file=sys.stderr)
        return EXIT_FAILED
    tokens = [request.method, request.path]
    if request.is_write:
        tokens += ["-H", f"X-Undo-Session:{undo_session}"]
    if request.body is not None:
        tokens += ["--json", json.dumps(request.body)]
    return api_cli.run(tokens, as_json=as_json, environ=environ, lock=lock)
