"""Shared service layer for Rekordbox <-> djay Pro sync (SYNC-01..06).

HTTP routes under ``/api/v1/rb-djay-sync`` and ``python -m apps.sync`` call
these functions directly. Nothing here shells out to a CLI.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from apps.shared import paths
from apps.shared.rekordbox_writeback import (
    RekordboxWritebackDisabled,
    require_writeback_enabled,
    writeback_enabled,
)
from apps.sync.playlist_apply import PlaylistApplyError, _load_plan
from apps.sync.playlist_diff import (
    DEFAULT_MATCHES,
    MAX_OPS_BEFORE_ABORT,
    generate_plan,
    write_diff_md,
    write_patch_csv,
    write_plan_json,
)

SYNC_DIR = paths.DATA_DIR / "sync"
DEFAULT_PLAN_PATH = SYNC_DIR / "playlist-plan.json"
HTTP_PLAYLIST_APPLY_SURFACE = "http.rb_djay_sync.playlists.apply"
HTTP_ANALYSIS_APPLY_SURFACE = "http.rb_djay_sync.analysis.apply"
HTTP_RATINGS_APPLY_SURFACE = "http.rb_djay_sync.ratings.apply"
HTTP_CUES_APPLY_SURFACE = "http.rb_djay_sync.cues.apply"


class RbDjaySyncError(RuntimeError):
    """Base error with a machine-readable ``code`` for HTTP mapping."""

    code: str = "RB_DJAY_SYNC_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(message)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def fingerprint_capability() -> dict[str, Any]:
    """Probe chromaprint without importing apps.sync.fingerprint at module level."""
    try:
        import acoustid  # noqa: PLC0415
    except Exception:
        return {
            "fingerprint_available": False,
            "reason": "pyacoustid/acoustid is not installed in this runtime",
        }
    if not hasattr(acoustid, "fingerprint_file"):
        return {
            "fingerprint_available": False,
            "reason": "acoustid import succeeded but fingerprint backend is unavailable",
        }
    return {"fingerprint_available": True, "reason": None}


def get_status() -> dict[str, Any]:
    """Return DB paths, artefact freshness, fingerprint capability, writeback gate."""
    artefacts = {
        name: {"path": str(path), "mtime": _mtime(path)}
        for name, path in {
            "matches.csv": SYNC_DIR / "matches.csv",
            "playlist-plan.json": SYNC_DIR / "playlist-plan.json",
            "analysis-diff.csv": SYNC_DIR / "analysis-diff.csv",
            "ratings-diff.csv": SYNC_DIR / "ratings-diff.csv",
            "cue-diff.csv": SYNC_DIR / "cue-diff.csv",
        }.items()
    }
    fp = fingerprint_capability()
    return {
        "rekordbox_live_db": str(paths.REKORDBOX_LIVE_DB),
        "rekordbox_working_db": str(paths.REKORDBOX_WORKING_DB),
        "djay_live_db": str(paths.DJAY_LIVE_DB),
        "djay_working_db": str(paths.DJAY_WORKING_DB),
        "sync_dir": str(SYNC_DIR),
        "artefacts": artefacts,
        "fingerprint_available": fp["fingerprint_available"],
        "fingerprint_reason": fp.get("reason"),
        "writeback_enabled": writeback_enabled(),
    }


def ensure_djay_working_copy() -> Path:
    target = paths.DJAY_WORKING_DB
    if target.exists():
        return target
    copied = paths.copy_live_dbs()
    resolved = copied.get("djay")
    if resolved is None or not Path(resolved).exists():
        raise RbDjaySyncError(
            f"djay working DB missing at {target} and live DB "
            f"{paths.DJAY_LIVE_DB} is absent",
            code="RB_DJAY_DB_MISSING",
        )
    return Path(resolved)


def list_djay_playlists() -> list[dict[str, Any]]:
    from apps.shared.djay_db import iter_playlists  # noqa: PLC0415

    db_path = ensure_djay_working_copy()
    return [
        {
            "uuid": pl.uuid,
            "name": pl.name,
            "track_count": len(pl.track_uuids),
            "track_uuids": list(pl.track_uuids),
        }
        for pl in iter_playlists(db_path)
    ]


def run_match(
    *,
    out_dir: Path | None = None,
    use_fingerprint: bool = False,
) -> dict[str, Any]:
    from apps.audit import match_rb_djay  # noqa: PLC0415

    if use_fingerprint:
        fp = fingerprint_capability()
        if not fp["fingerprint_available"]:
            raise RbDjaySyncError(
                fp.get("reason") or "fingerprint backend unavailable",
                code="FINGERPRINT_UNAVAILABLE",
            )
    resolved_out = out_dir or SYNC_DIR
    resolved_out.mkdir(parents=True, exist_ok=True)
    result = match_rb_djay.run(out_dir=resolved_out, use_fingerprint=use_fingerprint)
    matches_csv = resolved_out / "matches.csv"
    return {
        "matches_csv": str(matches_csv),
        "stats": dict(result.stats),
        "matched": len(result.matched),
        "review": len(result.review),
        "rb_only": len(result.rb_only),
        "djay_only": len(result.djay_only),
    }


def run_playlist_plan(
    *,
    matches_path: Path | None = None,
    only_playlists: list[str] | None = None,
    max_ops: int = MAX_OPS_BEFORE_ABORT,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    resolved_matches = matches_path or DEFAULT_MATCHES
    if not resolved_matches.exists():
        raise RbDjaySyncError(
            f"matches.csv missing: {resolved_matches}",
            code="MATCHES_CSV_MISSING",
        )
    only_set = set(only_playlists) if only_playlists else None
    plan = generate_plan(
        rb_db_path=None,
        djay_db_path=None,
        matches_path=resolved_matches,
        only_playlists=only_set,
    )
    op_total = sum(
        len(op.adds) + len(op.removes) + (1 if op.op == "create" else 0)
        for op in plan.playlists
    )
    if op_total > max_ops:
        raise RbDjaySyncError(
            f"plan contains {op_total} ops (> max_ops={max_ops})",
            code="PLAN_TOO_LARGE",
        )
    resolved_out = out_dir or SYNC_DIR
    resolved_out.mkdir(parents=True, exist_ok=True)
    plan_json = resolved_out / "playlist-plan.json"
    patch_csv = resolved_out / "playlist-patch.csv"
    diff_md = resolved_out / "playlist-diff.md"
    write_plan_json(plan, plan_json)
    write_patch_csv(plan, patch_csv)
    write_diff_md(plan, diff_md)
    totals = Counter(op.op for op in plan.playlists)
    return {
        "plan_path": str(plan_json),
        "patch_csv": str(patch_csv),
        "diff_md": str(diff_md),
        "op_total": op_total,
        "summary": {
            "create": totals.get("create", 0),
            "update": totals.get("update", 0),
            "noop": totals.get("noop", 0),
            "djay_only_playlists": len(plan.djay_only),
            "membership_adds": sum(len(op.adds) for op in plan.playlists),
            "membership_removes": sum(len(op.removes) for op in plan.playlists),
        },
    }


def _playlist_apply_summary(plan: dict, playlist_filter: set[str] | None) -> dict[str, Any]:
    totals = Counter()
    per_playlist: list[dict[str, Any]] = []
    for op in plan.get("playlists", []):
        name = op.get("rb_name", "")
        if playlist_filter is not None and name not in playlist_filter:
            continue
        totals[op.get("op", "noop")] += 1
        per_playlist.append(
            {
                "rb_name": name,
                "op": op.get("op"),
                "adds": len(op.get("adds", [])),
                "removes": len(op.get("removes", [])),
                "reordered": bool(op.get("reordered")),
            }
        )
    return {
        "dry_run": True,
        "totals": dict(totals),
        "playlists": per_playlist,
    }


def run_playlist_apply(
    *,
    dry_run: bool = True,
    plan_path: Path | None = None,
    playlists: list[str] | None = None,
    bulk: bool = False,
    live: bool = False,
    i_understand_the_risks: bool = False,
    allow_broken: bool = False,
) -> dict[str, Any]:
    if live and not i_understand_the_risks:
        raise RbDjaySyncError(
            "live playlist apply requires i_understand_the_risks=true",
            code="LIVE_WRITE_REFUSED",
        )
    if playlists and bulk:
        raise RbDjaySyncError(
            "playlists and bulk are mutually exclusive",
            code="RB_DJAY_SYNC_ERROR",
        )
    resolved_plan = plan_path or DEFAULT_PLAN_PATH
    try:
        plan = _load_plan(resolved_plan)
    except PlaylistApplyError as exc:
        raise RbDjaySyncError(str(exc), code="RB_DJAY_SYNC_ERROR") from exc

    playlist_filter: set[str] | None = None
    if playlists:
        playlist_filter = {name.strip() for name in playlists if name.strip()}

    if dry_run or not live:
        return _playlist_apply_summary(plan, None if bulk else playlist_filter)

    from apps.sync import playlist_apply as pa  # noqa: PLC0415

    try:
        pa._assert_djay_quit()
        pa._assert_rekordbox_quit()
    except PlaylistApplyError as exc:
        raise RbDjaySyncError(str(exc), code="RB_DJAY_SYNC_ERROR") from exc

    try:
        integrity_override = pa._integrity_gate(
            pa._live_integrity_report(),
            allow_broken=allow_broken,
            threshold=pa.DEFAULT_INTEGRITY_THRESHOLD,
        )
    except PlaylistApplyError as exc:
        raise RbDjaySyncError(str(exc), code="RB_DJAY_SYNC_ERROR") from exc

    db_path = pa._live_db_path(True)
    if not db_path.exists():
        raise RbDjaySyncError(
            f"live djay DB missing: {db_path}",
            code="RB_DJAY_DB_MISSING",
        )

    backup_path, ts = pa._backup_djay_db(db_path, pa.DEFAULT_BACKUP_DIR)
    rev = pa._write_reversal_script(
        backup_path, db_path, pa.DEFAULT_BACKUP_DIR, ts, integrity_override
    )
    effective_filter = None if bulk else playlist_filter
    result = pa.apply_plan(
        plan,
        db_path=db_path,
        playlist_filter=effective_filter,
        live=True,
    )
    result.backup_path = backup_path
    result.reversal_script = rev
    return {
        "dry_run": False,
        "backup_path": str(backup_path),
        "reversal_script": str(rev),
        "integrity_override": integrity_override,
        "per_playlist": [asdict(row) for row in result.per_playlist],
        "all_verified": result.all_verified,
    }


def run_metadata_plan(
    *,
    matches_path: Path | None = None,
    min_confidence: float = 0.70,
    prefer: Literal["rekordbox", "djay", "newest", "rb"] = "newest",
    include_cues: bool = False,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    from apps.audit import sync_diff  # noqa: PLC0415
    from apps.shared.djay_db import iter_analysis as djay_iter_analysis  # noqa: PLC0415
    from apps.shared.djay_db import iter_tracks as djay_iter_tracks  # noqa: PLC0415
    from apps.shared.rekordbox_db import iter_analysis as rb_iter_analysis  # noqa: PLC0415
    from apps.shared.rekordbox_db import iter_tracks as rb_iter_tracks  # noqa: PLC0415
    from apps.shared.rekordbox_db import open_db  # noqa: PLC0415

    resolved_matches = matches_path or DEFAULT_MATCHES
    if not resolved_matches.exists():
        raise RbDjaySyncError(
            f"matches.csv missing: {resolved_matches}",
            code="MATCHES_CSV_MISSING",
        )
    resolved_out = out_dir or SYNC_DIR
    resolved_out.mkdir(parents=True, exist_ok=True)

    djay_path = ensure_djay_working_copy()
    rb_db = open_db()
    try:
        rb_analysis = {a.uuid_or_id: a for a in rb_iter_analysis(rb_db)}
        rb_ratings = {str(t.id): (t.rating or 0) for t in rb_iter_tracks(rb_db)}
    finally:
        try:
            rb_db.close()
        except Exception:
            pass

    djay_analysis = {a.uuid_or_id: a for a in djay_iter_analysis(djay_path)}
    djay_ratings = {t.uuid: (t.rating or 0) for t in djay_iter_tracks(djay_path)}
    pairs = list(
        sync_diff.iter_match_pairs(resolved_matches, min_confidence=min_confidence)
    )
    resolved_prefer = "rb" if prefer == "rekordbox" else prefer
    analysis_rows, rating_rows = sync_diff.build_analysis_diff(
        rb_analysis,
        djay_analysis,
        rb_ratings,
        djay_ratings,
        pairs,
        prefer=resolved_prefer,
    )
    analysis_csv = resolved_out / "analysis-diff.csv"
    ratings_csv = resolved_out / "ratings-diff.csv"
    sync_diff.write_diff_csv(analysis_rows, analysis_csv)
    sync_diff.write_diff_csv(rating_rows, ratings_csv)

    cue_csv: str | None = None
    cue_row_count = 0
    if include_cues:
        cue_result = run_cues_plan(
            matches_path=resolved_matches,
            min_confidence=min_confidence,
            out_dir=resolved_out,
        )
        cue_csv = cue_result["cue_diff_csv"]
        cue_row_count = cue_result["row_count"]

    return {
        "analysis_diff_csv": str(analysis_csv),
        "ratings_diff_csv": str(ratings_csv),
        "analysis_row_count": len(analysis_rows),
        "ratings_row_count": len(rating_rows),
        "analysis_summary": sync_diff.summarise(analysis_rows),
        "ratings_summary": sync_diff.summarise(rating_rows),
        "cue_diff_csv": cue_csv,
        "cue_row_count": cue_row_count,
    }


def run_cues_plan(
    *,
    matches_path: Path | None = None,
    min_confidence: float = 0.70,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    from apps.audit import cue_comparison  # noqa: PLC0415
    from apps.shared.rekordbox_db import open_db  # noqa: PLC0415

    resolved_matches = matches_path or DEFAULT_MATCHES
    if not resolved_matches.exists():
        raise RbDjaySyncError(
            f"matches.csv missing: {resolved_matches}",
            code="MATCHES_CSV_MISSING",
        )
    resolved_out = out_dir or SYNC_DIR
    resolved_out.mkdir(parents=True, exist_ok=True)
    djay_path = ensure_djay_working_copy()
    rb_db = open_db()
    try:
        rows = cue_comparison.build_diff_rows(
            resolved_matches,
            rb_db,
            djay_path,
            min_confidence=min_confidence,
        )
    finally:
        try:
            rb_db.close()
        except Exception:
            pass
    cue_csv = resolved_out / "cue-diff.csv"
    cue_comparison.write_diff_csv(rows, cue_csv)
    return {
        "cue_diff_csv": str(cue_csv),
        "row_count": len(rows),
    }


def _summarise_csv_rows(rows: list[dict]) -> dict[str, dict[str, int]]:
    per_field: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        per_field[row.get("field", "rating")][row.get("resolution", "")] += 1
    return {field: dict(counts) for field, counts in per_field.items()}


def run_cues_apply(
    *,
    dry_run: bool = True,
    diff_csv: Path | None = None,
    live: bool = False,
    cautious: bool = False,
    bulk: bool = False,
    tracks: list[str] | None = None,
    i_understand_the_risks: bool = False,
) -> dict[str, Any]:
    from apps.sync import apply_cues  # noqa: PLC0415

    resolved_csv = diff_csv or (SYNC_DIR / "cue-diff.csv")
    rows = apply_cues._load_cue_diff(resolved_csv)
    if live and not i_understand_the_risks:
        raise RbDjaySyncError(
            "live cue apply requires i_understand_the_risks=true",
            code="LIVE_WRITE_REFUSED",
        )
    if dry_run or not live:
        return {
            "dry_run": True,
            "row_count": len(rows),
            "summary": {
                "djay_only_cues_add_to_rb": sum(
                    1 for r in rows if r.get("djay_only_positions")
                ),
                "rb_only_cues_add_to_djay": sum(
                    1 for r in rows if r.get("rb_only_positions")
                ),
                "conflicts": sum(1 for r in rows if r.get("conflicting_positions")),
            },
            "live_write_note": (
                "apply_cues live path is a safety scaffold only; cue bytes are not written (known issue D3)"
            ),
        }
    try:
        require_writeback_enabled("http.rb_djay_sync.cues.apply")
    except RekordboxWritebackDisabled as exc:
        raise RbDjaySyncError(exc.message, code="WRITEBACK_DISABLED") from exc
    only_tracks = set(tracks) if tracks else None
    apply_cues.live_run(
        rows,
        only_tracks=only_tracks,
        flag_ok=i_understand_the_risks,
        cautious=cautious,
        bulk=bulk,
    )
    return {
        "dry_run": False,
        "row_count": len(rows),
        "live_write_note": (
            "apply_cues live path exercised safety harness only; cue bytes were not written (known issue D3)"
        ),
    }


def run_analysis_apply(
    *,
    dry_run: bool = True,
    diff_csv: Path | None = None,
    live: bool = False,
    fields: list[str] | None = None,
    i_understand_the_risks: bool = False,
) -> dict[str, Any]:
    from apps.sync import apply_analysis  # noqa: PLC0415

    resolved_csv = diff_csv or (SYNC_DIR / "analysis-diff.csv")
    rows = apply_analysis._load_diff(resolved_csv)
    if live and not i_understand_the_risks:
        raise RbDjaySyncError(
            "live analysis apply requires i_understand_the_risks=true",
            code="LIVE_WRITE_REFUSED",
        )
    if dry_run or not live:
        return {
            "dry_run": True,
            "row_count": len(rows),
            "summary": _summarise_csv_rows(rows),
        }
    try:
        require_writeback_enabled("http.rb_djay_sync.analysis.apply")
    except RekordboxWritebackDisabled as exc:
        raise RbDjaySyncError(exc.message, code="WRITEBACK_DISABLED") from exc
    field_set = set(fields) if fields else None
    from apps.shared.rekordbox_db import open_db  # noqa: PLC0415
    from apps.sync.analysis_csv_live import live_run  # noqa: PLC0415

    rc = live_run(
        rows,
        fields=field_set,
        flag_ok=i_understand_the_risks,
        rb_db_path=paths.REKORDBOX_LIVE_DB,
        djay_db_path=paths.DJAY_LIVE_DB,
        open_rb_db=open_db,
    )
    return {"dry_run": False, "exit_code": rc, "row_count": len(rows)}


def run_ratings_apply(
    *,
    dry_run: bool = True,
    diff_csv: Path | None = None,
    live: bool = False,
    cautious: bool = False,  # noqa: ARG001 - kept for its keyword callers
    bulk: bool = False,  # noqa: ARG001 - kept for its keyword callers
    tracks: list[str] | None = None,
    i_understand_the_risks: bool = False,
) -> dict[str, Any]:
    from apps.sync import apply_ratings  # noqa: PLC0415

    resolved_csv = diff_csv or (SYNC_DIR / "ratings-diff.csv")
    rows = apply_ratings._load_ratings_diff(resolved_csv)
    if live and not i_understand_the_risks:
        raise RbDjaySyncError(
            "live ratings apply requires i_understand_the_risks=true",
            code="LIVE_WRITE_REFUSED",
        )
    if dry_run or not live:
        return {
            "dry_run": True,
            "row_count": len(rows),
            "summary": dict(Counter(r.get("resolution", "") for r in rows)),
        }
    try:
        require_writeback_enabled("http.rb_djay_sync.ratings.apply")
    except RekordboxWritebackDisabled as exc:
        raise RbDjaySyncError(exc.message, code="WRITEBACK_DISABLED") from exc
    only_tracks = set(tracks) if tracks else None
    apply_ratings.live_run(
        rows,
        rb_db_path=paths.REKORDBOX_LIVE_DB,
        djay_db_path=paths.DJAY_LIVE_DB,
        only_tracks=only_tracks,
        flag_ok=i_understand_the_risks,
    )
    return {"dry_run": False, "row_count": len(rows)}


def _resolve_stable_id(state_conn: sqlite3.Connection, rb_content_id: str) -> str:
    row = state_conn.execute(
        "SELECT stable_id FROM track_vendor_ids "
        "WHERE vendor = 'rekordbox' AND vendor_id = ? LIMIT 1",
        (rb_content_id,),
    ).fetchone()
    return row[0] if row else rb_content_id


def playlist_rb_djay_diff(
    *,
    playlist_name: str,
    plan_path: Path | None = None,
    state_db_path: Path | None = None,
) -> dict[str, Any]:
    """Map a saved playlist plan op into PlaylistDiff-shaped buckets."""
    resolved_plan = plan_path or DEFAULT_PLAN_PATH
    if not resolved_plan.exists():
        return {
            "computed": False,
            "reason": "no playlist plan artefact",
            "diff": {"rb_only": [], "djay_only": [], "both": [], "conflicts": []},
        }
    plan = json.loads(resolved_plan.read_text(encoding="utf-8"))
    canonical = playlist_name.casefold().strip()
    op = next(
        (
            item
            for item in plan.get("playlists", [])
            if str(item.get("rb_name", "")).casefold().strip() == canonical
        ),
        None,
    )
    if op is None:
        return {
            "computed": False,
            "reason": "playlist not present in saved plan",
            "diff": {"rb_only": [], "djay_only": [], "both": [], "conflicts": []},
        }

    state_path = state_db_path or paths.STATE_DB
    conn = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
    try:
        rb_only = [
            _resolve_stable_id(conn, item.get("rb_id", ""))
            for item in op.get("unmatched_rb", [])
        ]
        both = [
            _resolve_stable_id(conn, item.get("rb_id", ""))
            for item in op.get("target_members", [])
        ]
        djay_only = list(op.get("removes", []))
        conflicts: list[dict[str, Any]] = []
        if op.get("reordered"):
            conflicts.append(
                {
                    "stable_id": op.get("rb_id", ""),
                    "kind": "reordered",
                    "rb_position": None,
                    "djay_position": None,
                }
            )
    finally:
        conn.close()

    return {
        "computed": True,
        "generated_at": plan.get("generated_at"),
        "diff": {
            "rb_only": rb_only,
            "djay_only": djay_only,
            "both": both,
            "conflicts": conflicts,
        },
        "summary": {
            "op": op.get("op"),
            "adds": len(op.get("adds", [])),
            "removes": len(op.get("removes", [])),
            "unmatched_rb": len(op.get("unmatched_rb", [])),
        },
    }


__all__ = [
    "HTTP_ANALYSIS_APPLY_SURFACE",
    "HTTP_CUES_APPLY_SURFACE",
    "HTTP_PLAYLIST_APPLY_SURFACE",
    "HTTP_RATINGS_APPLY_SURFACE",
    "RbDjaySyncError",
    "fingerprint_capability",
    "get_status",
    "list_djay_playlists",
    "playlist_rb_djay_diff",
    "run_analysis_apply",
    "run_cues_apply",
    "run_cues_plan",
    "run_match",
    "run_metadata_plan",
    "run_playlist_apply",
    "run_playlist_plan",
    "run_ratings_apply",
]
