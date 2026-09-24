"""``python -m apps.genre_infer``: embed tracks, train a genre head on the library's tags, suggest.

    python -m apps.genre_infer embed --missing --limit 500     # CLAP, via uv (PEP 723)
    python -m apps.genre_infer train                           # prints held-out accuracy
    python -m apps.genre_infer suggest [--min-confidence 0.6]  # untagged tracks only
    python -m apps.genre_infer show ID

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

from . import store
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


def _cmd_embed(args: argparse.Namespace) -> int:
    data_dir, state, _master = _paths(args)
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
        ids = ids[: args.limit]
        paths = bulk_local_audio_paths(conn, ids)
    finally:
        conn.close()
    manifest = [{"stable_id": i, "path": str(p)} for i, p in paths.items() if p is not None]
    not_local = [i for i, p in paths.items() if p is None]
    if not manifest:
        print(
            f"nothing to embed ({len(not_local)} selected tracks have no local audio)",
            file=sys.stderr,
        )
        return 2
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
                str(args.threads),
            ],
            check=True,
        )
        rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
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
    doc = json.loads(path.read_text()) if path.is_file() else {"suggestions": {}}
    s = doc["suggestions"].get(args.stable_id)
    if s is None:
        print(f"no suggestion for {args.stable_id}", file=sys.stderr)
        return 1
    print(json.dumps({**s, "model_cv_accuracy": doc.get("cv_accuracy")}, indent=1))
    return 0


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
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
