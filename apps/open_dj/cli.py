"""``python -m apps.open_dj.cli`` -- open-dj reference CLI."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from apps.open_dj.canon import to_canonical_bytes
from apps.open_dj.diff import diff_documents, format_report
from apps.open_dj.validate import validate_document


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return 1
    return handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-dj-tool",
        description="open-dj v0.2 reference CLI (validate / canon / diff).",
    )
    sub = parser.add_subparsers(dest="command")

    p_val = sub.add_parser("validate", help="Validate a doc against the schema.")
    p_val.add_argument("path")
    p_val.set_defaults(handler=_cmd_validate)

    p_can = sub.add_parser("canon", help="Rewrite to canonical JCS form.")
    p_can.add_argument("path", help="Path to .open-dj.json, or '-' for stdin.")
    p_can.add_argument("--in-place", "-i", action="store_true")
    p_can.set_defaults(handler=_cmd_canon)

    p_diff = sub.add_parser("diff", help="Structural diff (track_id-keyed).")
    p_diff.add_argument("a")
    p_diff.add_argument("b")
    p_diff.set_defaults(handler=_cmd_diff)

    return parser


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        doc = _load_doc(args.path)
    except _IOError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    errors = validate_document(doc)
    if not errors:
        return 0
    for msg in errors:
        print(msg, file=sys.stderr)
    return 2


def _cmd_canon(args: argparse.Namespace) -> int:
    if args.path == "-":
        raw = sys.stdin.buffer.read()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"<stdin>: invalid JSON: {exc}", file=sys.stderr)
            return 1
        sys.stdout.buffer.write(to_canonical_bytes(doc))
        return 0

    try:
        doc = _load_doc(args.path)
    except _IOError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    canon_bytes = to_canonical_bytes(doc)

    if args.in_place:
        _atomic_write(Path(args.path), canon_bytes)
    else:
        sys.stdout.buffer.write(canon_bytes)
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    try:
        a = _load_doc(args.a)
        b = _load_doc(args.b)
    except _IOError as exc:
        print(exc.message, file=sys.stderr)
        return 2
    report = diff_documents(a, b)
    if report.is_empty:
        return 0
    print(format_report(report))
    return 1


class _IOError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _load_doc(path_str: str) -> dict:
    path = Path(path_str)
    if not path.exists():
        raise _IOError(f"{path_str}: no such file")
    try:
        with path.open("rb") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise _IOError(f"{path_str}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise _IOError(f"{path_str}: {exc}") from exc


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
