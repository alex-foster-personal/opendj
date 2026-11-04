"""Segment viewer: inspect song-structure sections over a waveform, for humans and agents.

Usage (repo root, app venv). Library mode is the default view; the Jamendo
corpus is the open regression set, shown collapsed:

    python -m scripts.structure.segment_viewer --library APPDATA --work DIR \\
        [--corpus DIR --audio DIR] serve [--port 8767]
    python -m scripts.structure.segment_viewer --library APPDATA --work DIR library-search "<words>"
    python -m scripts.structure.segment_viewer --library APPDATA --work DIR library-segments <stable_id>
    python -m scripts.structure.segment_viewer --library APPDATA --work DIR library-bench
    python -m scripts.structure.segment_viewer --library APPDATA --work DIR library-summary
    python -m scripts.structure.segment_viewer --library APPDATA --work DIR library-select-bench [--size 30]
    python -m scripts.structure.segment_viewer --library APPDATA --work DIR library-manifest OUT.jsonl
    python -m scripts.structure.segment_viewer --corpus DIR --audio DIR segments|tracks|summary|prepare

Every read command prints exactly the JSON the matching route serves
(``/api/library/...`` for the library, ``/api/tracks...`` and
``/api/summary`` for the corpus), so an agent reads what a human sees
without a server. ``APPDATA`` is the app's data folder holding
``master.plain.db`` and ``state/state.db`` (opened read-only); ``--work``
is where the viewer keeps peaks, All-In-One output and ``bench.json``.
``library-manifest`` writes the bench's ``{"stable_id", "path"}`` lines
for ``allin1_runner.py`` (local paths: never commit it).

-Claude
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any

from .corpus import Corpus, UnknownTrack
from .library import Library, select_bench
from .library import UnknownTrack as UnknownLibraryTrack

DEFAULT_PORT = 8767
HOST = "127.0.0.1"
SEARCH_LIMIT = 50
CORPUS_CMDS = ("segments", "tracks", "summary", "prepare")


def _warm_library(library: Library) -> None:
    """Score the bench in the background so the list fills; the open test set is computed on demand only."""
    for sid in library.bench_ids():
        library.segments(sid)


def _warm_corpus(corpus: Corpus) -> None:
    for sid in corpus.tracks:
        corpus.segments(sid)


def _serve(corpus: Corpus | None, library: Library | None, port: int) -> int:
    import uvicorn

    from .app import create_app

    if library is not None:
        threading.Thread(target=_warm_library, args=(library,), name="segment-viewer-warm", daemon=True).start()
    lib_n = len(library.tracks) if library else 0
    cor_n = len(corpus.tracks) if corpus else 0
    print(f"[OK] segment viewer on http://{HOST}:{port}/ (library {lib_n} tracks, corpus {cor_n})", flush=True)
    uvicorn.run(create_app(corpus, library), host=HOST, port=port, log_level="warning")
    return 0


def _print(doc: Any) -> None:
    print(json.dumps(doc, indent=1))


def _library_cmd(lib: Library, args: argparse.Namespace) -> int:
    if args.cmd == "library-search":
        _print({"tracks": lib.search(args.query, args.limit)})
    elif args.cmd == "library-segments":
        try:
            _print(lib.segments(args.stable_id))
        except UnknownLibraryTrack:
            print(f"[ERROR] unknown library track {args.stable_id}", file=sys.stderr)
            return 2
    elif args.cmd == "library-bench":
        _print({"tracks": lib.bench()})
    elif args.cmd == "library-summary":
        _print(lib.summary())
    elif args.cmd == "library-select-bench":
        sel = select_bench(lib, args.size, args.seed)
        lib.bench_path().write_text(json.dumps(sel, indent=1), encoding="utf-8", newline="\n")
        _print({k: v for k, v in sel.items() if k != "stable_ids"} | {"picked": len(sel["stable_ids"])})
        if len(sel["stable_ids"]) < args.size:
            print(f"[ERROR] only {len(sel['stable_ids'])} eligible tracks, asked for {args.size}", file=sys.stderr)
            return 1
    elif args.cmd == "library-manifest":
        lines = [json.dumps({"stable_id": sid, "path": str(lib.audio_path(sid))}) for sid in lib.bench_ids()]
        if not lines:
            print("[ERROR] no bench.json yet: run library-select-bench first", file=sys.stderr)
            return 1
        args.out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        print(f"[OK] {len(lines)} tracks -> {args.out}")
    else:
        raise AssertionError(f"unhandled command {args.cmd}")
    return 0


def _corpus_cmd(corpus: Corpus, args: argparse.Namespace) -> int:
    if args.cmd == "segments":
        try:
            _print(corpus.segments(args.stable_id))
        except UnknownTrack:
            print(f"[ERROR] unknown track {args.stable_id}", file=sys.stderr)
            return 2
    elif args.cmd == "tracks":
        _print({"tracks": corpus.listing()})
    elif args.cmd == "summary":
        _print(corpus.summary())
    elif args.cmd == "prepare":
        _warm_corpus(corpus)
        print(json.dumps(corpus.summary()))
    else:
        raise AssertionError(f"unhandled command {args.cmd}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="segment_viewer", description=__doc__.split("\n\n")[0])
    ap.add_argument("--library", type=Path, help="app data dir with master.plain.db and state/state.db")
    ap.add_argument("--work", type=Path, help="library-mode work dir: peaks/, allin1/, bench.json")
    ap.add_argument("--corpus", type=Path, help="open test set dir with corpus.jsonl, allin1/, peaks/")
    ap.add_argument("--audio", type=Path, help="dir the corpus manifest's audio files are in")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("segments").add_argument("stable_id")
    for name in ("tracks", "summary", "prepare", "library-bench", "library-summary"):
        sub.add_parser(name)
    search = sub.add_parser("library-search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=SEARCH_LIMIT)
    sub.add_parser("library-segments").add_argument("stable_id")
    sel = sub.add_parser("library-select-bench")
    sel.add_argument("--size", type=int, default=30)
    sel.add_argument("--seed", type=int, default=20261012)
    sub.add_parser("library-manifest").add_argument("out", type=Path)
    args = ap.parse_args(argv)

    if (args.library is None) != (args.work is None):
        ap.error("--library and --work go together")
    if (args.corpus is None) != (args.audio is None):
        ap.error("--corpus and --audio go together")
    library = Library.open(args.library, args.work) if args.library else None
    corpus = Corpus.open(args.corpus, args.audio) if args.corpus else None
    if args.cmd == "serve":
        if library is None and corpus is None:
            ap.error("serve needs --library/--work, --corpus/--audio, or both")
        return _serve(corpus, library, args.port)
    if args.cmd.startswith("library-"):
        if library is None:
            ap.error(f"{args.cmd} needs --library and --work")
        return _library_cmd(library, args)
    if args.cmd in CORPUS_CMDS:
        if corpus is None:
            ap.error(f"{args.cmd} needs --corpus and --audio")
        return _corpus_cmd(corpus, args)
    raise AssertionError(f"unhandled command {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
