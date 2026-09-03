"""Agent-native command line for Spotify to SoundCloud transfer.

Requirements:
✔︎ ✅ 🎯 Source and target are explicit in the command name.
✔︎ ✅ 🎯 Dry-run and live modes are mutually exclusive and mandatory.
✔︎ ✅ 🎯 Live writes require both risk acknowledgement and source-id confirmation.
✔︎ ✅ 🎯 The full three-bucket report is written atomically as JSON.

- if live mode lacks either safety acknowledgement then provider IO is broken
- if the output already exists without --overwrite then evidence preservation is broken
- if an API credential is absent then the command must fail before provider IO
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

from apps.spotify.client import MissingCredentialsError, SpotifyClient
from apps.spotify.url_parse import parse_playlist_identifier

from .config import TransferConfigurationError
from .llm import LlmMatchError, OpenRouterBatchMatcher
from .service import TransferContractError, execute_transfer
from .soundcloud import SoundCloudClient, SoundCloudError

EXIT_OK = 0
EXIT_SAFETY = 2
EXIT_CONFIGURATION = 3
EXIT_PROVIDER = 4


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.streaming_transfer",
        description="Transfer one Spotify playlist to SoundCloud with audited matching.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    transfer = commands.add_parser(
        "spotify-to-soundcloud",
        help="fetch Spotify, match via SoundCloud search, and optionally create a playlist",
    )
    transfer.add_argument("source", help="Spotify playlist URL, URI, or bare id")
    transfer.add_argument(
        "--confidence-threshold",
        type=float,
        required=True,
        help="minimum (0, 1] confidence for deterministic and LLM matches",
    )
    transfer.add_argument(
        "--output",
        type=Path,
        required=True,
        help="destination for the complete JSON transfer report",
    )
    transfer.add_argument(
        "--sharing",
        choices=("private", "public"),
        required=True,
        help="sharing mode if --live creates the SoundCloud playlist",
    )
    mode = transfer.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="search and match without target writes",
    )
    mode.add_argument("--live", action="store_true", help="create the matched SoundCloud playlist")
    transfer.add_argument(
        "--i-understand-the-risks",
        action="store_true",
        help="required with --live",
    )
    transfer.add_argument(
        "--confirm-playlist-id",
        help="required with --live and must exactly equal the parsed Spotify id",
    )
    transfer.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing report file atomically",
    )
    return parser


def _validate_safety(args: argparse.Namespace, playlist_id: str) -> str | None:
    if args.live and not args.i_understand_the_risks:
        return "--live requires --i-understand-the-risks"
    if args.live and args.confirm_playlist_id != playlist_id:
        return "--live requires --confirm-playlist-id equal to the parsed Spotify id"
    if args.dry_run and (args.i_understand_the_risks or args.confirm_playlist_id):
        return "live safety flags are invalid with --dry-run"
    if args.output.exists() and not args.overwrite:
        return f"output exists; pass --overwrite to replace it: {args.output}"
    return None


def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _run_spotify_to_soundcloud(args: argparse.Namespace) -> int:
    try:
        playlist_id = parse_playlist_identifier(args.source)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_SAFETY
    safety_error = _validate_safety(args, playlist_id)
    if safety_error is not None:
        print(f"ERROR: {safety_error}", file=sys.stderr)
        return EXIT_SAFETY

    try:
        llm = OpenRouterBatchMatcher.from_env()
        spotify = SpotifyClient.from_env()
        soundcloud = SoundCloudClient.from_env()
    except (
        LlmMatchError,
        MissingCredentialsError,
        SoundCloudError,
        TransferConfigurationError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIGURATION

    try:
        source = spotify.fetch_playlist(playlist_id, use_cache=False)
        if not source.tracks:
            print(
                "ERROR: Spotify source playlist contains no transferable tracks",
                file=sys.stderr,
            )
            return EXIT_PROVIDER
        report = execute_transfer(
            source,
            target=soundcloud,
            llm=llm,
            confidence_threshold=args.confidence_threshold,
            live=args.live,
            sharing=args.sharing,
        )
    except (httpx.HTTPError, LlmMatchError, SoundCloudError, TransferContractError) as exc:
        print(f"ERROR: transfer failed: {exc}", file=sys.stderr)
        return EXIT_PROVIDER
    finally:
        soundcloud.close()

    payload = report.as_dict()
    _write_report(args.output, payload)
    buckets = payload["buckets"]
    if not isinstance(buckets, dict):
        raise TransferContractError("report buckets are not an object")
    print(
        f"report={args.output} matched={len(buckets['matched'])} "
        f"unmatched={len(buckets['unmatched'])} "
        f"ungradable={len(buckets['ungradable'])} "
        f"live={'yes' if args.live else 'no'}"
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "spotify-to-soundcloud":
        return _run_spotify_to_soundcloud(args)
    raise TransferContractError(f"unsupported transfer command: {args.command!r}")
