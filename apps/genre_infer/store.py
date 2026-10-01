"""Where genre embeddings, the trained head and its suggestions live on disk.

``<data>/state/genre/``:

* ``embeddings.jsonl``: one line per (track, embedding model revision), the
  output of ``clap_runner.py`` verbatim. Append-only; the last line for a
  track and revision wins, so a re-embed never needs a rewrite.
* ``model.json``: the current :class:`~apps.genre_infer.classify.GenreModel`.
* ``suggestions.json``: ``{stable_id: {family, confidence, top}}`` plus the
  model digest and held-out accuracy it came from.

Machine-local and regenerable, like the structure and vocal caches: every
file here is derived from audio plus the library's own tags. A person's genre
tag is never written from here.

-Claude
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def genre_dir(data_dir: Path) -> Path:
    return data_dir / "state" / "genre"


def embeddings_path(data_dir: Path) -> Path:
    return genre_dir(data_dir) / "embeddings.jsonl"


def model_path(data_dir: Path) -> Path:
    return genre_dir(data_dir) / "model.json"


def suggestions_path(data_dir: Path) -> Path:
    return genre_dir(data_dir) / "suggestions.json"


def append_embeddings(data_dir: Path, rows: Iterable[dict[str, Any]]) -> int:
    path = embeddings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
            n += 1
    return n


def load_embeddings(
    data_dir: Path,
) -> tuple[dict[str, list[float]], dict[str, str], str | None, str | None]:
    """(vectors, failures, model, revision) for the most recent embedding revision on disk.

    Only one revision is ever trained on at a time: vectors from two model
    revisions do not share a space, so mixing them would be silently wrong.
    """
    path = embeddings_path(data_dir)
    if not path.is_file():
        return {}, {}, None, None
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows:
        return {}, {}, None, None
    model, revision = rows[-1]["model"], rows[-1]["revision"]
    vectors: dict[str, list[float]] = {}
    failures: dict[str, str] = {}
    for r in rows:
        if r["model"] != model or r["revision"] != revision:
            continue
        if r["status"] == "ok":
            vectors[r["stable_id"]] = r["vector"]
            failures.pop(r["stable_id"], None)
        else:
            failures[r["stable_id"]] = r.get("reason", "unknown")
            vectors.pop(r["stable_id"], None)
    return vectors, failures, model, revision


def write_json(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, path)


__all__ = [
    "append_embeddings",
    "embeddings_path",
    "genre_dir",
    "load_embeddings",
    "model_path",
    "suggestions_path",
    "write_json",
]
