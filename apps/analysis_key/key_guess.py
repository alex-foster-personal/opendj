"""The best-guess key of a track the analyzer declined to name (KEY-GUESS-01).

A `no_tonal_center` key lane is `failed` and, by the record contract, carries
no payload: the analyzer will not publish a key it is unsure of. The DJ still
wants its best candidate on screen, dimmed with a "?", rather than a blank
cell. So the producer keeps that candidate beside the lane, in the record's
free-form ``features_blob[KEY_GUESS_BLOB]``, and the library rows serve it as
``key_guess`` + ``key_guess_confidence``. The lane stays `failed`: no key sync,
harmonic match or smartlist ever reads a guess as a key.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 KEY-GUESS-01 an ambiguous key shows its best guess
    [if] the key lane fails no_tonal_center [then] the record keeps the best candidate's Camelot and confidence
    [if] the lane is ok [then] no guess is kept (the key itself is served)
    [if] a declined record predates the guess [then] the drain re-runs it once so it gains one

-Claude
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from apps.analysis.lanes import own_backend
from apps.analysis_key import canon, flags, profiles
from apps.analysis_key.version import LANE, PRODUCER, PRODUCER_VERSION

#: Key under ``AnalysisRecord.features_blob`` holding the guess.
KEY_GUESS_BLOB: str = "key_guess"
#: The own key producer's records, as ``apps.analysis.backends.own_key`` writes them.
KEY_BACKEND: str = own_backend(LANE, PRODUCER)
KEY_VERSION: str = PRODUCER_VERSION


def guess_blob(estimate: profiles.KeyEstimate, flag: flags.TonalCenterFlag) -> dict[str, Any]:
    """``features_blob`` for a key record: the guess when the lane declined, else empty."""
    if not flag.no_tonal_center:
        return {}
    return {
        KEY_GUESS_BLOB: {
            "camelot": canon.to_camelot(estimate.key),
            "openkey": canon.to_open_key(estimate.key),
            "confidence": round(float(estimate.confidence), 4),
            "margin": round(float(flag.margin), 4),
        }
    }


def load_key_guesses(
    db_path: Path, stable_ids: Sequence[str], *, backend: str, version: str
) -> dict[str, tuple[str, float]]:
    """stable_id -> (camelot, confidence) for every id whose current key record holds a guess."""
    if not stable_ids or not db_path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out: dict[str, tuple[str, float]] = {}
    try:
        for i in range(0, len(stable_ids), 500):
            sub = list(stable_ids[i:i + 500])
            rows = conn.execute(
                "SELECT stable_id, "
                f"json_extract(record_json, '$.features_blob.{KEY_GUESS_BLOB}.camelot'), "
                f"json_extract(record_json, '$.features_blob.{KEY_GUESS_BLOB}.confidence') "
                f"FROM analysis WHERE backend = ? AND backend_version = ? "
                f"AND stable_id IN ({','.join('?' * len(sub))}) "
                f"AND json_extract(record_json, '$.features_blob.{KEY_GUESS_BLOB}.camelot') IS NOT NULL",
                (backend, version, *sub),
            ).fetchall()
            out.update({sid: (str(cam), float(conf)) for sid, cam, conf in rows})
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}
        raise
    finally:
        conn.close()
    return out


def needs_guess_rerun(lane: str, record_json: str) -> bool:
    """True for a no_tonal_center key record saved before guesses existed: it is not current.

    The queue runner's skip check and the drain's ``done_ids`` must agree, or the
    drain offers a track the runner skips as ``already_current_version``.
    """
    if lane != "key":
        return False
    record = json.loads(record_json)
    key_lane = (record.get("lanes") or {}).get("key") or {}
    return (
        key_lane.get("status") == "failed"
        and str(key_lane.get("reason") or "").startswith("no_tonal_center")
        and KEY_GUESS_BLOB not in (record.get("features_blob") or {})
    )


def declined_without_guess_sql() -> str:
    """WHERE fragment (on ``analysis``) for a no_tonal_center key record saved before guesses existed."""
    return (
        "json_extract(record_json, '$.lanes.key.status') = 'failed' "
        "AND json_extract(record_json, '$.lanes.key.reason') LIKE 'no_tonal_center%' "
        f"AND json_extract(record_json, '$.features_blob.{KEY_GUESS_BLOB}') IS NULL"
    )
