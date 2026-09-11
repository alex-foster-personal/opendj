"""The three storage subcommands of ``python -m apps.lyrics`` (D13.2).

Parser wiring and dispatch for ``ingest-state``, ``migrate-legacy-words`` and
``purge``, in their own module so ``apps/lyrics/__main__.py`` stays under the
600-line gate. The bodies live in :mod:`apps.lyrics.ingest_state`,
:mod:`apps.lyrics.legacy_words` and :mod:`apps.lyrics.purge`; the PR-3 batch
driver imports ``ingest_state`` directly and never goes through here.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from apps.lyrics import ingest_state, legacy_words, purge
from apps.lyrics.artifacts import asset_clients_for_mode
from apps.lyrics.ingest_state import MATCH_MODES

STORAGE_COMMANDS: frozenset[str] = frozenset({"ingest-state", "migrate-legacy-words", "purge"})


def add_storage_commands(subcommands: argparse._SubParsersAction) -> None:
    """Register the three storage subcommands on ``subcommands``."""
    ingest = subcommands.add_parser("ingest-state", help="load a bench run into state.db")
    ingest.add_argument("--manifest", type=Path, required=True, help="bench manifest JSON")
    ingest.add_argument("--coverage", type=Path, help="vocal-presence.json")
    ingest.add_argument(
        "--match-by", choices=MATCH_MODES, default="vendor-id", help="how tracks join state.db"
    )
    ingest.add_argument("--db-path", type=Path, help="state DB (default: the repo data dir)")
    ingest.add_argument("--write", action="store_true", help="write; omit for a dry run")

    migrate = subcommands.add_parser(
        "migrate-legacy-words", help="convert renamed-aside branch-era lyric tables"
    )
    migrate.add_argument("--db-path", type=Path, help="state DB (default: the repo data dir)")
    migrate.add_argument("--dry-run", action="store_true", help="report only, write nothing")

    purge_cmd = subcommands.add_parser(
        "purge", help="remove one provider's lyrics from row, disk and R2"
    )
    purge_cmd.add_argument("--source", required=True, help="source prefix, matched LIKE prefix%%")
    purge_cmd.add_argument("--db-path", type=Path, help="state DB (default: the repo data dir)")
    purge_cmd.add_argument("--dry-run", action="store_true", help="report only, write nothing")


def cmd_storage(args: argparse.Namespace) -> int:
    """The three subcommands that write state.db + the karaoke_words artifact."""
    if args.command == "ingest-state":
        s3, cfg = asset_clients_for_mode(writing=args.write)
        return ingest_state.main(
            manifest=args.manifest, coverage=args.coverage, write=args.write,
            match_by=args.match_by, db_path=args.db_path, s3=s3, cfg=cfg,
        )
    elif args.command == "migrate-legacy-words":  # noqa: RET505 - explicit elif is the house style
        s3, cfg = asset_clients_for_mode(writing=not args.dry_run)
        return legacy_words.main(
            db_path=args.db_path, dry_run=args.dry_run, s3=s3, cfg=cfg
        )
    elif args.command == "purge":
        s3, cfg = asset_clients_for_mode(writing=not args.dry_run)
        return purge.main(
            source_prefix=args.source, db_path=args.db_path, dry_run=args.dry_run,
            s3=s3, cfg=cfg,
        )
    else:
        raise AssertionError(f"unhandled storage command {args.command!r}")


__all__ = ["STORAGE_COMMANDS", "add_storage_commands", "cmd_storage"]
