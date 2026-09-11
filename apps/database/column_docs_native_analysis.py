"""Column docs for native-analysis v1's three tables.

Split out of ``column_docs.py`` for the same reason
:mod:`apps.database.column_docs_analysis_retention` was (the 600-line-per-
Python-file limit); ``column_docs.py`` merges this dict into ``COLUMN_DOCS``,
so callers still read one map.

Schema authority: ``apps/analysis/store.py`` (``_ANALYSIS_TABLES_SQL``),
mirrored into the consolidated engine schema's ``native_analysis_v1`` domain.
Design: ``specs/native-analysis-v1.md`` section 3.

-Claude
"""

from __future__ import annotations

NATIVE_ANALYSIS_COLUMN_DOCS: dict[str, dict[str, str]] = {
"analysis_canonical": {
    "stable_id": "Track this pointer is for. With `lane`, the primary key.",
    "lane": (
        "Selection lane: beatgrid, key, waveform, loudness or vocal. The "
        "unit source selection works in; never per track."
    ),
    "backend": (
        "Winning producer's backend name, `own_<lane>.<producer>`. Bench "
        "candidates (`own_<lane>.cand.<name>`) are never eligible."
    ),
    "backend_version": "Winning producer's semver. The primary ranking key.",
    "updated_at": (
        "UTC RFC 3339. Frozen when a recompute picks the same row again, "
        "because this stamp reaches the track's public updated_at and ETag."
    ),
},
"analysis_projection": {
    "stable_id": "Track this scalar is for. With `field`, the primary key.",
    "field": (
        "bpm, key, loudness_lufs, loudness_dbtp, key_change_count or "
        "tempo_change_count."
    ),
    "value": (
        "The scalar, NULL when status is not ok. Deliberately a typeless "
        "column so a REAL bpm stays REAL and a TEXT camelot stays TEXT; "
        "TEXT affinity would make every numeric smartlist filter lexical."
    ),
    "status": (
        "ok, failed or missing. `failed` means the analyzer ran and could "
        "not measure; `missing` means it has not run. A consumer renders "
        "either inert with the reason, never as a blank cell."
    ),
    "reason": (
        "The lane's named failure reason (no_trackable_pulse, not_decoded, "
        "no_tonal_center, ...) when status is not ok, else NULL."
    ),
    "confidence": "Producer's confidence for this scalar, when it reports one.",
    "backend": "Canonical producer that supplied it, mirroring analysis_canonical.",
    "backend_version": "That producer's semver.",
    "updated_at": (
        "UTC RFC 3339. Frozen when a rebuild produces an identical row, "
        "for the same ETag reason as analysis_canonical.updated_at."
    ),
},
"analysis_source_default": {
    "lane": "Selection lane. Primary key; a row exists only once promoted.",
    "source": "rbx or own. Absent row means rbx (D1: rbx until promotion).",
    "updated_at": "UTC RFC 3339 of the promotion.",
},
"analysis_queue_batch": {
    "batch_id": "One enqueue call. Primary key; `qb_` plus 16 hex.",
    "created_at": "UTC RFC 3339 of the enqueue.",
    "updated_at": "UTC RFC 3339 of the last state or plan change.",
    "state": "queued, running, cancelled or done.",
    "workers": (
        "Concurrency the admission rule chose from the LONGEST ADMITTED "
        "track, never from core count. 0 means nothing was admitted."
    ),
    "band": (
        "under_20_min, 20_to_45_min, over_45_min, or empty. Recorded so a "
        "round log can say which band a wall-clock figure belongs to."
    ),
    "memory_model": (
        "JSON of the MemoryModel the batch was budgeted under: floor_mb, "
        "slope_mb_per_min, and where those numbers were measured. Stored "
        "per batch so a re-measured model cannot retroactively rewrite what "
        "an older batch claimed."
    ),
    "note": "Free text saying why the batch exists (bump, cascade, manual).",
    "active_runner_id": (
        "Runner id of the process currently holding the exclusive batch lease, "
        "or NULL when no drain is active."
    ),
    "active_runner_pid": (
        "OS pid of the live drain process holding the batch lease. A second "
        "runner refuses takeover while this pid is alive."
    ),
},
"analysis_queue_item": {
    "batch_id": "Owning batch. With stable_id and lane, the primary key.",
    "stable_id": "Track to analyze.",
    "lane": "Lane to produce: beatgrid, key, waveform, loudness or vocal.",
    "backend": "Producer to run, `own_<lane>.<producer>`.",
    "file_path": (
        "Resolved local path the runner decodes, through the same path map "
        "the backlog drain uses. Empty for a refused item, which has no "
        "resolvable bytes."
    ),
    "duration_s": (
        "Audio length the admission rule budgeted with, from "
        "tracks.duration_ms. NULL means unknown, which is refused rather "
        "than guessed."
    ),
    "predicted_peak_mb": "floor + slope x minutes under the batch's model.",
    "state": (
        "pending, running, done, skipped, failed, refused or cancelled. "
        "done/skipped/failed/refused are terminal: a resume never revisits "
        "them."
    ),
    "reason": (
        "Named cause for a non-`done` terminal state: the admission refusal "
        "reason, the backend's per-track error, or already_current_version."
    ),
    "attempts": "Claims so far. A kill-and-resume shows 2, not 1.",
    "enqueued_at": "UTC RFC 3339 the item entered the queue.",
    "started_at": "UTC RFC 3339 of the current claim; NULL when pending.",
    "finished_at": "UTC RFC 3339 the item reached a terminal state.",
    "runner_id": (
        "Runner holding the claim. NULL for anything not in flight, which "
        "is what makes an abandoned claim identifiable after a kill."
    ),
},
"analysis_stale": {
    "stable_id": "Track whose record went stale.",
    "lane": "Lane of the stale record.",
    "backend": "Stale record's backend. With the version, names one row.",
    "backend_version": "Stale record's producer semver.",
    "reason": "Why: today only dependency_record_changed.",
    "detected_at": "UTC RFC 3339 the cascade marked it.",
},}
