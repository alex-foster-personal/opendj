"""Stem A/B/C labelling tasks: payload validation, storage, and batch consensus (#11193).

Domain logic for ``stems_ab_rank`` micro-tasks. HTTP lives in
``apps.webui.server.routes.label_tasks``. Clips must resolve to validated
bundles via :func:`apps.stems.artifacts.validate_label_stem_clip`.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apps.stems.artifacts import (
    stem_label_clip_api_path,
    validate_label_stem_clip,
)

MODALITY_STEMS_AB_RANK: str = "stems_ab_rank"
LABEL_BATCH_MIN: int = 3
LABEL_BATCH_MAX: int = 5
STEMS_AB_CLIP_IDS: tuple[str, ...] = ("A", "B", "C")


class LabelTaskError(ValueError):
    """Malformed task payload or response for a labelling task."""


@dataclass(frozen=True)
class StemAbClip:
    clip_id: str
    stable_id: str
    part: str = "vocals"


@dataclass(frozen=True)
class LabelTaskRow:
    id: str
    experiment_id: str
    modality: str
    payload: dict[str, Any]
    reward_karma: int
    max_wait_s: int
    batch_size: int
    created_at: datetime


def batch_size_allowed(batch_size: int) -> bool:
    return LABEL_BATCH_MIN <= batch_size <= LABEL_BATCH_MAX


def parse_iso_timestamp(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_clips(payload: dict[str, Any]) -> tuple[StemAbClip, ...]:
    raw = payload.get("clips")
    if not isinstance(raw, list) or len(raw) != 3:
        raise LabelTaskError("stems_ab_rank payload must list exactly three clips")
    clips: list[StemAbClip] = []
    seen_ids: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise LabelTaskError("each clip must be an object")
        clip_id = entry.get("id")
        stable_id = entry.get("stable_id")
        part = entry.get("part", "vocals")
        if not isinstance(clip_id, str) or clip_id not in STEMS_AB_CLIP_IDS:
            raise LabelTaskError("clip id must be one of A, B, C")
        if not isinstance(stable_id, str) or not stable_id.strip():
            raise LabelTaskError("clip stable_id must be a non-empty string")
        if not isinstance(part, str) or not part.strip():
            raise LabelTaskError("clip part must be a non-empty string")
        if clip_id in seen_ids:
            raise LabelTaskError("clip ids must be unique within the task")
        seen_ids.add(clip_id)
        clips.append(StemAbClip(clip_id=clip_id, stable_id=stable_id.strip(), part=part.strip()))
    if seen_ids != set(STEMS_AB_CLIP_IDS):
        raise LabelTaskError("clips must include ids A, B, and C")
    return tuple(clips)


def parse_stems_ab_payload(payload: dict[str, Any]) -> tuple[StemAbClip, ...]:
    if payload.get("kind") not in (None, MODALITY_STEMS_AB_RANK):
        raise LabelTaskError("payload kind must be stems_ab_rank")
    return _parse_clips(payload)


def stems_ab_clip_ids_for_arms(arm_count: int) -> tuple[str, ...]:
    if arm_count < len(STEMS_AB_CLIP_IDS):
        raise LabelTaskError("stems_ab_rank needs at least three experiment arms")
    return STEMS_AB_CLIP_IDS


def load_task(conn: sqlite3.Connection, task_id: str) -> LabelTaskRow:
    row = conn.execute(
        """
        SELECT id, experiment_id, modality, payload, reward_karma, max_wait_s, batch_size, created_at
        FROM label_task WHERE id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise KeyError(task_id)
    payload = json.loads(row[3])
    if not isinstance(payload, dict):
        raise LabelTaskError("task payload must be a JSON object")
    batch_size = int(row[6])
    if not batch_size_allowed(batch_size):
        raise LabelTaskError(f"batch_size must be between {LABEL_BATCH_MIN} and {LABEL_BATCH_MAX}")
    return LabelTaskRow(
        id=row[0],
        experiment_id=row[1],
        modality=row[2],
        payload=payload,
        reward_karma=int(row[4]),
        max_wait_s=int(row[5]),
        batch_size=batch_size,
        created_at=parse_iso_timestamp(row[7]),
    )


def resolve_clip_urls(
    clips: Sequence[StemAbClip],
    *,
    stems_dir: Path,
    roots: Sequence[Path] | None,
    api_prefix: str = "/api/v1",
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for clip in clips:
        validate_label_stem_clip(clip.stable_id, clip.part, stems_dir=stems_dir, roots=roots)
        out.append(
            {
                "id": clip.clip_id,
                "stable_id": clip.stable_id,
                "part": clip.part,
                "url": stem_label_clip_api_path(clip.stable_id, clip.part, api_prefix=api_prefix),
            }
        )
    return out


def validate_ranking(ranking: list[Any], clip_ids: set[str]) -> list[str]:
    if not isinstance(ranking, list) or len(ranking) != len(clip_ids):
        raise LabelTaskError("ranking must list every clip id exactly once")
    seen: set[str] = set()
    ordered: list[str] = []
    for item in ranking:
        if not isinstance(item, str) or item not in clip_ids:
            raise LabelTaskError("ranking entries must be known clip ids")
        if item in seen:
            raise LabelTaskError("ranking must not repeat a clip id")
        seen.add(item)
        ordered.append(item)
    if seen != clip_ids:
        raise LabelTaskError("ranking must include every clip id")
    return ordered


def insert_response(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    anon_session: str,
    ranking: list[str],
    strength: float,
    cost_choice: str,
    ms_to_complete: int,
    user_id: str | None = None,
    created_at: str | None = None,
) -> str:
    if not anon_session.strip():
        raise LabelTaskError("anon_session is required")
    if not cost_choice.strip():
        raise LabelTaskError("cost_choice is required")
    if strength < 0.0 or strength > 1.0:
        raise LabelTaskError("strength must be between 0 and 1")
    if ms_to_complete < 0:
        raise LabelTaskError("ms_to_complete must be non-negative")
    task = load_task(conn, task_id)
    if task.modality != MODALITY_STEMS_AB_RANK:
        raise LabelTaskError(f"unsupported modality {task.modality!r}")
    clips = parse_stems_ab_payload(task.payload)
    clip_ids = {c.clip_id for c in clips}
    validate_ranking(ranking, clip_ids)
    response_id = str(uuid.uuid4())
    stamp = created_at or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00"
    conn.execute(
        """
        INSERT INTO label_response (
            id, task_id, user_id, anon_session, ranking, strength, cost_choice,
            ms_to_complete, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            response_id,
            task_id,
            user_id,
            anon_session,
            json.dumps(ranking),
            strength,
            cost_choice,
            ms_to_complete,
            stamp,
        ),
    )
    conn.commit()
    return response_id


def _borda_aggregate(rankings: Sequence[Sequence[str]]) -> list[str]:
    scores: Counter[str] = Counter()
    n = len(rankings[0]) if rankings else 0
    for ranking in rankings:
        for idx, clip_id in enumerate(ranking):
            scores[clip_id] += n - idx
    return [clip_id for clip_id, _ in scores.most_common()]


def _plurality(values: Sequence[str]) -> str:
    counts = Counter(values)
    winner, count = counts.most_common(1)[0]
    if count == 1 and len(counts) > 1 and all(c == 1 for c in counts.values()):
        raise LabelTaskError("cost_choice tie with no plurality")
    return winner


def build_consensus(
    task: LabelTaskRow,
    responses: Sequence[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    clock = now or datetime.now(tz=UTC)
    n = len(responses)
    base = {
        "task_id": task.id,
        "experiment_id": task.experiment_id,
        "response_count": n,
        "batch_size": task.batch_size,
        "max_wait_s": task.max_wait_s,
        "wait_elapsed_s": max(0, int((clock - task.created_at).total_seconds())),
    }
    if n < task.batch_size:
        return {
            **base,
            "status": "insufficient_labels",
            "reason": "fewer responses than batch_size",
        }
    rankings = [json.loads(row["ranking"]) if isinstance(row["ranking"], str) else row["ranking"] for row in responses]
    strengths = [float(row["strength"]) for row in responses]
    costs = [str(row["cost_choice"]) for row in responses]
    try:
        cost_winner = _plurality(costs)
    except LabelTaskError:
        return {
            **base,
            "status": "insufficient_labels",
            "reason": "cost_choice had no plurality",
        }
    return {
        **base,
        "status": "ready",
        "consensus": {
            "ranking": _borda_aggregate(rankings),
            "strength": sum(strengths) / len(strengths),
            "cost_choice": cost_winner,
        },
    }


def fetch_responses(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ranking, strength, cost_choice, anon_session, created_at
        FROM label_response WHERE task_id = ? ORDER BY created_at
        """,
        (task_id,),
    ).fetchall()
    return [
        {
            "ranking": row[0],
            "strength": row[1],
            "cost_choice": row[2],
            "anon_session": row[3],
            "created_at": row[4],
        }
        for row in rows
    ]


def wait_window_expired(task: LabelTaskRow, *, now: datetime | None = None) -> bool:
    clock = now or datetime.now(tz=UTC)
    return clock >= task.created_at + timedelta(seconds=task.max_wait_s)


__all__ = [
    "LABEL_BATCH_MAX",
    "LABEL_BATCH_MIN",
    "MODALITY_STEMS_AB_RANK",
    "STEMS_AB_CLIP_IDS",
    "LabelTaskError",
    "LabelTaskRow",
    "StemAbClip",
    "batch_size_allowed",
    "build_consensus",
    "fetch_responses",
    "insert_response",
    "load_task",
    "parse_stems_ab_payload",
    "resolve_clip_urls",
    "stems_ab_clip_ids_for_arms",
    "validate_ranking",
    "wait_window_expired",
]
