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
},}
