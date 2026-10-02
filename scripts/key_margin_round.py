"""Key margin round: the own Krumhansl estimate against each file's tag key.

    uv run --locked --extra analysis python -m scripts.key_margin_round \
        --db <data-dir>/state/state.db --limit 60

Reads, never writes: one JSON line per track that has a tag key (a
``track_fields.key`` row) and resolvable audio, then a summary of exact
agreement and how many tracks each candidate ``MARGIN_THRESHOLD`` keeps. The
tag key is MIK's or rekordbox's, so this measures agreement with the tool the
user already trusts, not ground truth. Round 1 is logged in
``specs/native-analysis-v1.md`` ("Key lane round 1").
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import librosa
import numpy as np

from apps.analysis_key import canon, profiles

THRESHOLDS: tuple[float, ...] = (0.0, 0.002, 0.005, 0.01, 0.02)


def measure(db: Path, limit: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT t.stable_id, t.file_path, json_extract(f.value_json, '$') FROM tracks t "
        "JOIN track_fields f ON f.stable_id = t.stable_id AND f.field_name = 'key' "
        "ORDER BY t.stable_id LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    out: list[dict[str, Any]] = []
    for stable_id, path, tag in rows:
        if not path or not Path(path).exists():
            continue
        samples, rate = librosa.load(path, sr=None, mono=True)
        chroma = librosa.feature.chroma_cqt(y=samples, sr=rate)
        order = np.argsort(profiles.profile_correlations(chroma))[::-1]
        estimate = profiles.estimate_key_krumhansl(chroma)
        out.append({
            "stable_id": stable_id,
            "tag": tag,
            "own": canon.to_camelot(profiles.key_for_index(int(order[0]))),
            "second": canon.to_camelot(profiles.key_for_index(int(order[1]))),
            "confidence": round(float(estimate.confidence), 4),
            "margin": round(float(estimate.margin), 5),
        })
        print(json.dumps(out[-1]), flush=True)
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise SystemExit("[ERROR] no track had both a tag key and resolvable audio")
    by_threshold = {}
    for threshold in THRESHOLDS:
        kept = [row for row in rows if row["margin"] >= threshold]
        exact = sum(row["own"] == row["tag"] for row in kept)
        by_threshold[str(threshold)] = {
            "kept": len(kept), "of": len(rows),
            "exact_pct": round(100 * exact / len(kept), 1) if kept else None,
        }
    margins = sorted(row["margin"] for row in rows)
    return {"n": len(rows), "margin_median": margins[len(margins) // 2], "by_threshold": by_threshold}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--limit", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps({"summary": summarize(measure(args.db, args.limit))}))


if __name__ == "__main__":
    main()
