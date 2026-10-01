"""S12 CloudSync first + no-op capture against a running hub.

Copies the engine library into a new temp spoke (new machine_id, empty
watermark) so the first ``run_sync`` classifies as ``first`` without rotating
the live hub. Prefer a throwaway capture hub: this enrolls a temporary
machine_id. Never calls ``rotate``.

``run_sync`` is called in-process with the default ``HttpTransport`` so
``PhaseTimer`` output is readable. Do not pass ``transport=``.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from apps.sync_hub.client import run_sync, state_db_path
from scripts.perf.capture_kpi_ledger import (
    S12_METHOD,
    S12_REQUIRED,
    S12_UNIT,
    CaptureMeta,
    build_row,
    error_row,
)


def _s12_error(meta: CaptureMeta, reason: str) -> list[dict[str, Any]]:
    return [
        error_row(kpi=kpi, unit=S12_UNIT, method=S12_METHOD, meta=meta, reason=reason)
        for kpi in S12_REQUIRED
    ]


def _copy_state_db(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    for suffix in ("-wal", "-shm"):
        extra = Path(str(src) + suffix)
        if extra.is_file():
            shutil.copy2(extra, Path(str(dest) + suffix))


def _prepare_spoke(src_data_dir: Path, dest: Path) -> int:
    """Copy state.db, clear watermarks, do not copy machine-id (new id)."""
    src_db = state_db_path(src_data_dir)
    if not src_db.is_file():
        raise FileNotFoundError(f"engine library missing at {src_db}")
    dest_db = state_db_path(dest)
    _copy_state_db(src_db, dest_db)
    conn = sqlite3.connect(str(dest_db))
    try:
        conn.execute("DELETE FROM sync_state")
        conn.commit()
        row = conn.execute("SELECT count(*) FROM tracks").fetchone()
        return 0 if row is None else int(row[0])
    finally:
        conn.close()


def _kind_reason(label: str, result: Any) -> str:
    timings = getattr(result, "timings", None)
    kind = getattr(timings, "kind", None) if timings is not None else None
    return (
        f"{label} sync kind={kind!r} pushed={result.pushed} "
        f"pulled={result.pulled} rounds={result.rounds}"
    )


def _first_ok(result: Any) -> bool:
    return result.timings is not None and result.timings.kind == "first"


def _noop_ok(result: Any) -> bool:
    return (
        result.timings is not None
        and result.timings.kind == "noop"
        and result.pushed == 0
        and result.pulled == 0
        and result.rounds == 1
    )


def _phase_rows(meta: CaptureMeta, first: Any, track_count: int) -> list[dict[str, Any]]:
    timings = first.timings
    note = f"kind=first; denominator={track_count} tracks; sha={meta.sha}"
    return [
        build_row(
            kpi=name,
            value=value,
            unit=S12_UNIT,
            method=S12_METHOD,
            meta=meta,
            note=note,
        )
        for name, value in (
            ("cloudsync_hello_s", timings.hello_s),
            ("cloudsync_push_s", timings.push_s),
            ("cloudsync_pull_s", timings.pull_s),
            ("cloudsync_digest_s", timings.digest_s),
            ("cloudsync_local_digest_s", timings.local_digest_s),
        )
    ]


def _success_rows(
    meta: CaptureMeta, first: Any, noop: Any, track_count: int
) -> list[dict[str, Any]]:
    denom = f"kind={{kind}}; denominator={track_count} tracks; sha={meta.sha}"
    rows = [
        build_row(
            kpi="cloudsync_first_sync_s",
            value=first.timings.total_s,
            unit=S12_UNIT,
            method=S12_METHOD,
            meta=meta,
            note=denom.format(kind="first"),
        ),
        build_row(
            kpi="cloudsync_noop_sync_s",
            value=noop.timings.total_s,
            unit=S12_UNIT,
            method=S12_METHOD,
            meta=meta,
            note=denom.format(kind="noop"),
        ),
    ]
    rows.extend(_phase_rows(meta, first, track_count))
    return rows


def _sync_pair(
    tmp_path: Path, hub_url: str, meta: CaptureMeta, track_count: int
) -> list[dict[str, Any]]:
    spoke_name = f"perf-capture-{meta.capture_id}"
    try:
        first = run_sync(tmp_path, hub_url, name=spoke_name)
    except Exception as exc:
        return _s12_error(meta, f"run_sync first raised {type(exc).__name__}: {exc}")
    if not _first_ok(first):
        return _s12_error(meta, _kind_reason("first", first))
    try:
        noop = run_sync(tmp_path, hub_url, name=spoke_name)
    except Exception as exc:
        return _s12_error(meta, f"run_sync noop raised {type(exc).__name__}: {exc}")
    if not _noop_ok(noop):
        return _s12_error(meta, _kind_reason("noop", noop))
    return _success_rows(meta, first, noop, track_count)


def _preflight(hub_url: str | None, data_dir: Path | None, track_count: int | None) -> str | None:
    if not hub_url:
        return "S12 requires --hub; missing hub URL"
    if data_dir is None:
        return "data-dir could not be resolved from health.state_db.path; pass --data-dir"
    if track_count is None:
        return "health.state_db.tracks is missing"
    return None


def capture(
    *,
    hub_url: str | None,
    meta: CaptureMeta,
    data_dir: Path | None,
    track_count: int | None,
) -> list[dict[str, Any]]:
    reason = _preflight(hub_url, data_dir, track_count)
    if reason is not None:
        return _s12_error(meta, reason)
    assert hub_url is not None and data_dir is not None and track_count is not None
    tmp = tempfile.mkdtemp(prefix="perf-capture-spoke-")
    tmp_path = Path(tmp)
    try:
        try:
            copied = _prepare_spoke(data_dir, tmp_path)
        except (OSError, sqlite3.Error) as exc:
            return _s12_error(meta, f"failed to copy engine library: {exc}")
        if copied != track_count:
            return _s12_error(
                meta,
                f"spoke copy track count {copied} disagrees with "
                f"health.state_db.tracks {track_count}",
            )
        return _sync_pair(tmp_path, hub_url, meta, track_count)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
