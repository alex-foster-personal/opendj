"""CLI: ``python -m apps.sync.usb <subcommand>``.

Subcommands
-----------

``snapshot SOURCE DEST [--allow-unsupported]``
    Hard-link the stick tree at SOURCE into a new DEST and write
    ``DEST.manifest.json`` beside it (relative path, size and SHA-256 per
    regular file). Prints a JSON summary. A symlink, FIFO, socket or device
    in SOURCE is refused (exit 1) unless ``--allow-unsupported``, which lists
    them under ``unsupported`` and leaves them out of the snapshot.

``snapshot-verify DEST [--manifest PATH]``
    Re-hash DEST against its manifest. Prints a JSON report; exit 0 only when
    every file matches and nothing is missing, replaced or extra, else 1.

``restore SNAPSHOT DEST [--manifest PATH]``
    Restore a snapshot into DEST (missing or an empty directory) after checking
    the snapshot against its manifest, then byte-compare DEST with SNAPSHOT.
    Prints a JSON receipt carrying ``byte_compare``; exit 0 only when the
    restored tree is byte-identical, else 1 with the run's own files removed.

Refusals print ``{"error", "message", "entries"}`` to stderr and exit 1;
bad arguments exit 2. Requirements: USBEXP-12729 (snapshot, snapshot-verify),
USBEXP-12737 (restore).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.sync.usb import snapshot as snap
from apps.sync.usb import snapshot_restore as restore

USAGE = """usage: python -m apps.sync.usb <subcommand> [args]

subcommands:
  snapshot          hard-linked snapshot of a stick tree plus a SHA-256 manifest
  snapshot-verify   re-hash a snapshot against its manifest
  restore           restore a snapshot into an empty root and byte-compare it
"""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m apps.sync.usb")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="hard-linked snapshot plus SHA-256 manifest")
    s.add_argument("source", type=Path, help="stick tree to snapshot (read only)")
    s.add_argument("dest", type=Path, help="new snapshot directory (must not exist)")
    s.add_argument(
        "--allow-unsupported",
        action="store_true",
        help="list symlinks and special files as unsupported instead of refusing",
    )
    v = sub.add_parser("snapshot-verify", help="re-hash a snapshot against its manifest")
    v.add_argument("dest", type=Path, help="snapshot directory")
    v.add_argument("--manifest", type=Path, default=None, help="manifest path (default beside)")
    r = sub.add_parser("restore", help="restore a snapshot into an empty root and byte-compare it")
    r.add_argument("snapshot", type=Path, help="snapshot directory (read only)")
    r.add_argument("dest", type=Path, help="restore root: missing or an empty directory")
    r.add_argument("--manifest", type=Path, default=None, help="manifest path (default beside)")
    return p


def _refuse(exc: snap.SnapshotError) -> int:
    payload = {"error": exc.code, "message": exc.message, "entries": exc.details}
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)
    return 1


def _restore(args: argparse.Namespace) -> int:
    try:
        receipt = restore.restore_snapshot(args.snapshot, args.dest, manifest=args.manifest)
    except restore.RestoreError as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
    return 0 if receipt.ok else 1


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    if not args_in:
        print(USAGE)
        return 0
    if args_in[0] not in {"snapshot", "snapshot-verify", "restore", "-h", "--help"}:
        print(f"unknown subcommand: {args_in[0]}\n{USAGE}", file=sys.stderr)
        return 2
    args = _parser().parse_args(args_in)
    if args.cmd == "restore":
        return _restore(args)
    try:
        if args.cmd == "snapshot":
            result = snap.create_snapshot(args.source, args.dest, allow_unsupported=args.allow_unsupported)
            summary = {
                "snapshot": str(result.dest),
                "manifest": str(result.manifest_path),
                "manifest_sha256": result.manifest_sha256,
                "files": result.file_count,
                "dirs": result.dir_count,
                "unsupported": result.unsupported,
            }
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0
        report = snap.verify_snapshot(args.dest, args.manifest)
    except snap.SnapshotError as exc:
        return _refuse(exc)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
