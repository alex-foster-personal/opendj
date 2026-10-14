"""CLI twins of the ``/api/v1/genres`` routes (agent-native parity, GENRE-06).

    python -m apps.genre_infer [--data-dir D] status
    python -m apps.genre_infer findings ID [ID ...]
    python -m apps.genre_infer attach ID --provider beatport --genre "Tech House" --confidence 1 --source URL
    python -m apps.genre_infer remove ID --provider beatport
    python -m apps.genre_infer infer (--stable-id ID ... | --all-missing [--limit N]) [--force]
    python -m apps.genre_infer similar ID [--limit 20]
    python -m apps.genre_infer config [--set key=<json> ...]
    python -m apps.genre_infer write-tag ID | undo-tag WRITE_ID | tag-journal [--stable-id ID]
    python -m apps.genre_infer import-search-cache PATH

Every command prints JSON on stdout. Exit 2: bad input or a refused write;
exit 3: a key is missing (the message names it) or every inference call failed.

-Claude
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apps.analysis import store as analysis_store

from . import findings, pipeline, providers, service, tag_write

EXIT_REFUSED = 2
EXIT_UNAVAILABLE = 3


def _print(doc: Any) -> None:
    print(json.dumps(doc, indent=1, ensure_ascii=False, default=str))


def _run(args: argparse.Namespace, fn: Callable[[Any], Any]) -> int:
    conn = analysis_store.open_conn(args.state_path)
    try:
        findings.ensure_schema(conn)
        conn.commit()
        _print(fn(conn))
    except providers.ProviderUnavailable as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except (findings.GenreInputError, service.UnknownTrack, tag_write.TagWriteRefused, KeyError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        conn.close()
    return 0


def _run_limit(text: str) -> int:
    """A per-run track cap of 1 to MAX_IDS; a negative slice bound would select almost everything."""
    value = int(text)
    if not 1 <= value <= service.MAX_IDS:
        raise argparse.ArgumentTypeError(f"--limit must be 1 to {service.MAX_IDS}, got {text!r}")
    return value


def _infer(args: argparse.Namespace, conn: Any) -> dict[str, Any]:
    if bool(args.stable_id) == args.all_missing:
        raise findings.GenreInputError("give --stable-id or --all-missing, not both")
    if len(args.stable_id) > service.MAX_IDS:
        raise findings.GenreInputError(f"at most {service.MAX_IDS} --stable-id values per run, got {len(args.stable_id)}")
    ids = args.stable_id or pipeline.missing_fused_ids(conn, pipeline.eligible_ids(conn, master_path=args.master_path))[
        : args.limit
    ]
    summary = (
        pipeline.infer_tracks(conn, ids, force=args.force, keys=pipeline.required_keys())
        if ids
        else pipeline.RunSummary()
    )
    doc = summary.as_dict()
    if not args.verbose:
        doc.pop("results")
    print(
        f"genre: {doc['done']} done, {doc['up_to_date']} up to date, {doc['skipped']} skipped, {doc['failed']} failed; "
        f"{doc['asked']} asked, {doc['shared_with_duplicates']} shared a duplicate's ask; "
        f"{doc['paid_searches']} paid searches (${doc['search_cost_usd']:.4f}); total ${doc['cost_usd']:.4f}; "
        f"per 1k asked: ${doc['cost_per_1k_usd']} total, ${doc['model_cost_per_1k_usd']} models only",
        file=sys.stderr,
    )
    attempted = summary.count(pipeline.OUTCOME_DONE) + summary.count(pipeline.OUTCOME_FAILED)
    if attempted and summary.count(pipeline.OUTCOME_FAILED) == attempted:
        raise providers.ProviderUnavailable("every call failed; see the reasons above")
    return doc


def _config(args: argparse.Namespace, conn: Any) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep:
            raise findings.GenreInputError(f"--set {item!r} must be key=<json>")
        try:
            patch[key] = json.loads(value)
        except ValueError as exc:
            raise findings.GenreInputError(f"--set {key}: {value!r} is not JSON") from exc
    cfg = findings.update_config(conn, patch) if patch else findings.read_config(conn)
    conn.commit()
    return cfg.as_dict()


def _import_search_cache(args: argparse.Namespace, conn: Any) -> dict[str, Any]:
    added = findings.import_search_cache(conn, Path(args.path), providers.QUERY_VERSION)
    conn.commit()
    return {"added": added}


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    def add(name: str, fn: Callable[[argparse.Namespace, Any], Any]) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        p.set_defaults(func=lambda a: _run(a, lambda c: fn(a, c)), needs_wheel=False)
        return p

    add("status", lambda _a, c: service.status(c))
    p = add("findings", lambda a, c: service.genres_for(c, a.stable_ids, master_path=a.master_path))
    p.add_argument("stable_ids", nargs="+")
    p = add(
        "attach",
        lambda a, c: service.attach_finding(
            c,
            stable_id=a.stable_id,
            provider=a.provider,
            genre=a.genre,
            confidence=a.confidence,
            source=a.source,
            note=a.note,
        ),
    )
    p.add_argument("stable_id")
    p.add_argument("--provider", required=True)
    p.add_argument("--genre", required=True)
    p.add_argument("--confidence", type=float, required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--note")
    p = add("remove", lambda a, c: {"removed": service.remove_finding(c, stable_id=a.stable_id, provider=a.provider)})
    p.add_argument("stable_id")
    p.add_argument("--provider", required=True)
    p = add("infer", _infer)
    p.add_argument("--stable-id", action="append", default=[])
    p.add_argument("--all-missing", action="store_true")
    p.add_argument("--limit", type=_run_limit, default=service.MAX_IDS)
    p.add_argument("--force", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p = add("similar", lambda a, c: service.similar(c, a.stable_id, a.limit))
    p.add_argument("stable_id")
    p.add_argument("--limit", type=int, default=20)
    p = add("config", _config)
    p.add_argument("--set", action="append", default=[])
    p = add("write-tag", lambda a, c: service.write_chosen_tag(c, data_dir=a.data_path, stable_id=a.stable_id, master_path=a.master_path))
    p.add_argument("stable_id")
    p = add("undo-tag", lambda a, c: tag_write.undo(c, a.write_id).__dict__)
    p.add_argument("write_id", type=int)
    p = add("tag-journal", lambda a, c: {"writes": tag_write.journal(c, a.stable_id)})
    p.add_argument("--stable-id")
    p = add("import-search-cache", _import_search_cache)
    p.add_argument("path")


__all__ = ["EXIT_REFUSED", "EXIT_UNAVAILABLE", "register"]
