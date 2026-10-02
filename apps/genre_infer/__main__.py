"""``python -m apps.genre_infer``: embed tracks, train a genre head on the library's tags, suggest.

    python -m apps.genre_infer embed --missing --limit 500     # CLAP, via uv (PEP 723)
    python -m apps.genre_infer train                           # prints held-out accuracy
    python -m apps.genre_infer suggest [--min-confidence 0.6]  # untagged tracks only
    python -m apps.genre_infer show ID
    python -m apps.genre_infer jev [--limit 500]                # JEV guesses, untagged tracks only

Labels are the wheel's simple genre FAMILIES (``apps/library_wheel/genre_families.py``)
of each track's tag, resolved with the wheel's own precedence
(``genre_tags_by_stable_id``). Tracks whose tag maps to no family are the ones
that get suggestions; a tagged track is never overwritten.

-Claude
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from apps.adapters.rekordbox import config as rb_config
from apps.library_wheel.genre_families import simple_genre_family
from apps.library_wheel.query import genre_tags_by_stable_id
from apps.shared.state import db as state_db
from apps.shared.state.locations import bulk_local_audio_paths

from . import jev, jev_store, store
from .classify import GenreModel, suggest, train

RUNNER = Path(__file__).with_name("clap_runner.py")


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    data_dir = Path(args.data_dir) if args.data_dir else rb_config.DATA_DIR
    state = Path(args.state_db) if args.state_db else data_dir / "state" / "state.db"
    master = Path(args.master_db) if args.master_db else data_dir / "master.plain.db"
    return data_dir, state, master


def _family_labels(state: Path, master: Path) -> tuple[dict[str, str], list[str]]:
    """(labels by stable_id, untagged-or-unmapped stable_ids)."""
    labels: dict[str, str] = {}
    unlabeled: list[str] = []
    for sid, tag in genre_tags_by_stable_id(state, master).items():
        fam = simple_genre_family(tag)
        if fam is None:
            unlabeled.append(sid)
        else:
            labels[sid] = fam[0]
    return labels, unlabeled


def _untagged(state: Path, master: Path) -> list[str]:
    """stable_ids with no genre tag anywhere: neither rekordbox nor the local genre field."""
    conn = state_db.open_ro(state)
    try:
        local = jev_store.local_genre_ids(conn)
    finally:
        conn.close()
    tags = genre_tags_by_stable_id(state, master)
    return sorted(sid for sid, tag in tags.items() if not (tag or "").strip() and sid not in local)


def _embed_targets(args: argparse.Namespace, state: Path, data_dir: Path) -> dict[str, Path | None]:
    """Selected stable_ids with their local audio path, None where the audio is not here."""
    vectors, failures, _m, _r = store.load_embeddings(data_dir)
    conn = state_db.open_ro(state)
    try:
        ids = list(args.stable_id) or [
            r[0]
            for r in conn.execute(
                "SELECT stable_id FROM tracks WHERE deleted_at IS NULL ORDER BY stable_id"
            )
        ]
        if args.missing:
            ids = [i for i in ids if i not in vectors and i not in failures]
        return dict(bulk_local_audio_paths(conn, ids[: args.limit]))
    finally:
        conn.close()


def _run_clap(manifest: list[dict[str, str]], threads: int) -> list[dict]:
    """Run the PEP 723 CLAP runner over ``manifest`` and return its rows."""
    with tempfile.TemporaryDirectory(prefix="clap-") as tmp:
        mf, out = Path(tmp) / "m.jsonl", Path(tmp) / "out.jsonl"
        mf.write_text("\n".join(json.dumps(m) for m in manifest))
        subprocess.run(
            [
                "uv",
                "run",
                "--no-project",
                "--script",
                str(RUNNER),
                "--manifest",
                str(mf),
                "--out",
                str(out),
                "--threads",
                str(threads),
            ],
            check=True,
        )
        return [json.loads(line) for line in out.read_text().splitlines() if line.strip()]


def _cmd_embed(args: argparse.Namespace) -> int:
    data_dir, state, _master = _paths(args)
    paths = _embed_targets(args, state, data_dir)
    manifest = [{"stable_id": i, "path": str(p)} for i, p in paths.items() if p is not None]
    not_local = [i for i, p in paths.items() if p is None]
    if not manifest:
        print(
            f"nothing to embed ({len(not_local)} selected tracks have no local audio)",
            file=sys.stderr,
        )
        return 2
    rows = _run_clap(manifest, args.threads)
    store.append_embeddings(data_dir, rows)
    ok = sum(r["status"] == "ok" for r in rows)
    print(
        f"embedded {ok} of {len(manifest)} local tracks; "
        f"{len(not_local)} selected had no local audio",
        file=sys.stderr,
    )
    return 0 if ok else 1


def _cmd_train(args: argparse.Namespace) -> int:
    data_dir, state, master = _paths(args)
    vectors, failures, model_name, revision = store.load_embeddings(data_dir)
    if not vectors:
        print(
            "no embeddings on disk; run `python -m apps.genre_infer embed` first", file=sys.stderr
        )
        return 2
    labels, _unlabeled = _family_labels(state, master)
    model = train(
        vectors,
        labels,
        embedding_model=str(model_name),
        embedding_revision=str(revision),
        min_per_class=args.min_per_class,
    )
    store.write_json(store.model_path(data_dir), json.loads(model.to_json()))
    n = sum(model.train_counts.values())
    print(
        json.dumps(
            {
                "trained_on": n,
                "embedded": len(vectors),
                "embed_failures": len(failures),
                "classes": model.train_counts,
                "dropped_classes": model.dropped_classes,
                "cv_accuracy": model.cv_accuracy,
                "cv_macro_f1": model.cv_macro_f1,
                "folds": model.meta.get("folds"),
            },
            indent=1,
        )
    )
    return 0


def _cmd_suggest(args: argparse.Namespace) -> int:
    data_dir, state, master = _paths(args)
    mp = store.model_path(data_dir)
    if not mp.is_file():
        print("no trained model; run `python -m apps.genre_infer train` first", file=sys.stderr)
        return 2
    model = GenreModel.from_json(mp.read_text())
    vectors, _f, name, revision = store.load_embeddings(data_dir)
    if (name, revision) != (model.embedding_model, model.embedding_revision):
        print(
            f"model was trained on {model.embedding_model}@{model.embedding_revision}, "
            f"embeddings on disk "
            f"are {name}@{revision}; retrain first",
            file=sys.stderr,
        )
        return 2
    _labels, unlabeled = _family_labels(state, master)
    targets = {sid: vectors[sid] for sid in unlabeled if sid in vectors}
    out = {
        sid: {"family": s.genre, "confidence": s.confidence, "top": s.top}
        for sid, s in suggest(model, targets).items()
        if s.confidence >= args.min_confidence
    }
    store.write_json(
        store.suggestions_path(data_dir),
        {
            "schema": "genre-suggestions/v1",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "model_digest": model.trainset_digest,
            "cv_accuracy": model.cv_accuracy,
            "min_confidence": args.min_confidence,
            "suggestions": out,
        },
    )
    print(
        f"{len(out)} suggestions at confidence >= {args.min_confidence} "
        f"for {len(targets)} embedded, "
        f"untagged tracks ({len(unlabeled)} untagged in total)",
        file=sys.stderr,
    )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    data_dir, _s, _m = _paths(args)
    path = store.suggestions_path(data_dir)
    doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"suggestions": {}}
    s = doc["suggestions"].get(args.stable_id)
    if s is None:
        print(f"no suggestion for {args.stable_id}", file=sys.stderr)
        return 1
    print(json.dumps({**s, "model_cv_accuracy": doc.get("cv_accuracy")}, indent=1))
    return 0


def _tag_doc(tags: list[jev.TagQuestion]) -> list[dict[str, str]]:
    return [{"name": t.name, "question": t.question} for t in tags]


def _answered(data_dir: Path, tags: list[jev.TagQuestion]) -> set[str]:
    """Tracks already answered (status ok) under these exact tag questions."""
    doc = jev_store.load_suggestions(data_dir)
    if doc.get("tags", []) != _tag_doc(tags):
        return set()
    return {sid for sid, r in doc["suggestions"].items() if isinstance(r, dict) and r.get("status") == "ok"}


def _jev_write(data_dir: Path, results: dict, tags: list[jev.TagQuestion], min_confidence: float) -> list[str]:
    """Merge this run into the sidecar (its answers replace earlier ones); return served models.

    Earlier answers are kept only while they answered the same tag questions:
    their tag probabilities mean nothing under renamed or reworded questions.
    """
    served = sorted({str(r.get("model")) for r in results.values() if r["status"] == "ok"})
    previous = jev_store.load_suggestions(data_dir)
    kept = previous.get("suggestions", {}) if previous.get("tags", []) == _tag_doc(tags) else {}
    merged = {**kept, **results}
    store.write_json(
        jev_store.suggestions_path(data_dir),
        {
            "schema": jev.SCHEMA,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "model": served,
            "min_confidence": min_confidence,
            "tags": _tag_doc(tags),
            "suggestions": merged,
        },
    )
    return served


def _jev_report(results: dict, served: list[str], untagged: int, min_confidence: float) -> int:
    """Print counts, UNKNOWN reasons and cost; exit 3 when every call failed."""
    unknown = {sid: r["reason"] for sid, r in results.items() if r["status"] != "ok"}
    doc = {"suggestions": results, "min_confidence": min_confidence}
    shown = sum(1 for sid in results if jev_store.genre_guess(doc, sid))
    cost = sum(float(r.get("cost") or 0.0) for r in results.values())
    print(
        f"asked JEV about {len(results)} untagged tracks ({untagged} untagged in total): "
        f"{len(results) - len(unknown)} answered, {len(unknown)} UNKNOWN, "
        f"{shown} confident enough to show (>= {min_confidence}); "
        f"model {', '.join(served) or 'none'}; cost ${cost:.5f}",
        file=sys.stderr,
    )
    if unknown:
        reasons = sorted(set(unknown.values()))
        print(f"UNKNOWN reasons: {'; '.join(reasons[:5])}", file=sys.stderr)
    # Every call failing is a measurement failure, not an empty result.
    return 3 if results and len(unknown) == len(results) else 0


def _cmd_jev(args: argparse.Namespace) -> int:
    """Ask JEV for a genre family (and the user's tag questions) for untagged tracks."""
    data_dir, state, master = _paths(args)
    tags = jev.load_tag_questions(jev_store.tags_path(data_dir))
    untagged = _untagged(state, master)
    # Only tracks with no genre tag at all: a tag the wheel cannot map is still
    # a tag, and its row would never serve the guess (GENRE-02).
    allowed = set(untagged)
    if args.stable_id:
        ids = [sid for sid in args.stable_id if sid in allowed]
    else:
        # Skip tracks already answered, so each run's --limit batch moves on to
        # new tracks instead of paying for the same first batch again.
        done = _answered(data_dir, tags)
        ids = [sid for sid in untagged if sid not in done]
    conn = state_db.open_ro(state)
    try:
        facts = jev_store.track_facts(conn, ids[: args.limit])
    finally:
        conn.close()
    results = jev.classify({sid: jev.build_state(f) for sid, f in facts.items()}, tags, workers=args.workers)
    served = _jev_write(data_dir, results, tags, args.min_confidence)
    return _jev_report(results, served, len(untagged), args.min_confidence)


def _positive_int(text: str) -> int:
    """A call cap of at least 1; a negative slice bound would select almost every track."""
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive integer")
    return value


def _probability(text: str) -> float:
    """A display floor in [0, 1]; NaN or out of range would expose every guess."""
    value = float(text)
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a probability in [0, 1]")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m apps.genre_infer")
    ap.add_argument("--data-dir")
    ap.add_argument("--state-db")
    ap.add_argument("--master-db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("embed")
    e.add_argument("--stable-id", action="append", default=[])
    e.add_argument("--missing", action="store_true")
    e.add_argument("--limit", type=int, default=500)
    e.add_argument("--threads", type=int, default=2)
    e.set_defaults(func=_cmd_embed)
    t = sub.add_parser("train")
    t.add_argument("--min-per-class", type=int, default=8)
    t.set_defaults(func=_cmd_train)
    s = sub.add_parser("suggest")
    s.add_argument("--min-confidence", type=float, default=0.6)
    s.set_defaults(func=_cmd_suggest)
    sh = sub.add_parser("show")
    sh.add_argument("stable_id")
    sh.set_defaults(func=_cmd_show)
    j = sub.add_parser("jev")
    j.add_argument("--stable-id", action="append", default=[])
    j.add_argument("--limit", type=_positive_int, default=500)
    j.add_argument("--min-confidence", type=_probability, default=jev.DEFAULT_MIN_CONFIDENCE)
    j.add_argument("--workers", type=int, default=8)
    j.set_defaults(func=_cmd_jev)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
