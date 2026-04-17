"""Rekordbox -> state.db ingest adapter (D5 / Phase 5 Plan 03).

Reads a Rekordbox 6/7 master.db via pyrekordbox and writes tracks,
provenance-wrapped analysed fields (``bpm``, ``key``, ``rating``),
vendor id links, and playlists to the state layer.

Safety:

* Always opens the RB DB read-only -- no writes possible.
* Dry-run by default (the outer SAVEPOINT is ROLLBACK-released).
* Default source is ``paths.REKORDBOX_WORKING_DB`` (the snapshot copy,
  never the live DB).
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as _dt
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from apps.shared import paths as shared_paths
from apps.shared.state import db as state_db
from apps.shared.state import ids as state_ids
from apps.shared.state import paths as state_paths
from apps.shared.state.writer import StateWriter, compute_playlist_id

_log = logging.getLogger(__name__)

_ClockFn = Callable[[], _dt.datetime]


@dataclasses.dataclass
class IngestReport:
    """Counters emitted by :func:`ingest_rb` for CLI + tests."""

    tracks_inserted: int = 0
    tracks_updated: int = 0
    tracks_unchanged: int = 0
    tracks_skipped: int = 0
    tier_counts: dict[str, int] = dataclasses.field(
        default_factory=lambda: {"isrc": 0, "fingerprint": 0, "inferred": 0}
    )
    playlists_inserted: int = 0
    playlists_updated: int = 0
    fields_written: int = 0
    duration_s: float = 0.0
    rb_path: str = ""
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rb_rows(rb_db: Any) -> Iterator[dict[str, Any]]:
    """Yield raw DjmdContent attributes we need (ISRC, Length, KeyName,
    Rating, updated_at) without editing the shared ``rekordbox_db.py``
    helper (which the M2 orchestrator also owns)."""
    for row in rb_db.get_content():
        folder_path = row.FolderPath or ""
        streaming = folder_path.startswith(
            ("spotify:", "tidal:", "http://", "https://")
        )
        bpm_raw = _safe_int(row.BPM)
        # Rekordbox stores BPM as an int*100 (e.g. 12800 = 128.0 BPM). A raw
        # value of 0 is ambiguous (unanalysed tracks and genuine 0.0 share
        # the encoding) but we preserve it as 0.0 rather than dropping to
        # None so downstream reporting can distinguish "known zero" from
        # "missing". Only a None row value (column absent / non-numeric)
        # maps back to None. See .planning/FAN-OUT-V2-TRIAGE-2026-04-17.md.
        bpm_value = bpm_raw / 100.0 if bpm_raw is not None else None
        length_s = _safe_int(getattr(row, "Length", None))
        duration_ms = length_s * 1000 if length_s else None

        artist_name = getattr(row.Artist, "Name", "") if row.Artist else ""
        album_name = getattr(row.Album, "Name", "") if row.Album else ""
        key_obj = getattr(row, "Key", None)
        key_name = None
        if key_obj is not None:
            key_name = getattr(key_obj, "ScaleName", None) or getattr(
                key_obj, "Name", None
            )

        yield {
            "id": str(row.ID),
            "title": row.Title or "",
            "artist": artist_name or "",
            "album": album_name or "",
            "folder_path": folder_path,
            "is_streaming": streaming,
            "isrc": ((row.ISRC or "").strip() or None),
            "bpm": bpm_value,
            "rating": _safe_int(getattr(row, "Rating", None)),
            "duration_ms": duration_ms,
            "file_size": _safe_int(getattr(row, "FileSize", None)),
            "key_name": key_name,
            "updated_at": getattr(row, "updated_at", None),
        }


def _rb_playlists(rb_db: Any) -> Iterator[tuple[str, str, list[str]]]:
    for p in rb_db.get_playlist():
        songs = list(getattr(p, "Songs", []) or [])
        songs.sort(key=lambda s: (getattr(s, "TrackNo", 0) or 0))
        tids = [
            str(s.ContentID)
            for s in songs
            if getattr(s, "ContentID", None) is not None
        ]
        yield (str(p.ID), p.Name or "", tids)


_UNKNOWN_MODIFIED_AT = "1970-01-01T00:00:00+00:00"


def _rb_modified_at(raw: Any) -> str:
    """Normalise ``DjmdContent.updated_at`` to an RFC 3339 UTC string.

    Deterministic fallback for missing values: we return the epoch so that
    ingesting the same RB snapshot twice is a true no-op (no history churn).
    """
    if raw is None:
        return _UNKNOWN_MODIFIED_AT
    if isinstance(raw, _dt.datetime):
        dt = raw if raw.tzinfo is not None else raw.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc).isoformat()
    try:
        parsed = _dt.datetime.fromisoformat(str(raw))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.astimezone(_dt.timezone.utc).isoformat()
    except ValueError:
        return _UNKNOWN_MODIFIED_AT


def ingest_rb(
    writer: StateWriter,
    rb_db_path: Path,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    clock: _ClockFn | None = None,
) -> IngestReport:
    """Ingest the Rekordbox DB at ``rb_db_path`` into the state DB.

    ``dry_run=True`` wraps everything in a SAVEPOINT that is
    ROLLBACK-released at the end. ``dry_run=False`` releases the
    SAVEPOINT to persist.
    """
    from pyrekordbox import Rekordbox6Database

    start = time.perf_counter()
    report = IngestReport(rb_path=str(rb_db_path), dry_run=dry_run)
    now_fn = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))

    rb_db = Rekordbox6Database(path=str(rb_db_path), unlock=False)
    # [I2] Use the public ``raw_conn`` accessor so this outer SAVEPOINT
    # composes with StateWriter's internal SAVEPOINTs without reaching into
    # private attributes.
    conn = writer.raw_conn
    savepoint = "phase5_ingest_rb"

    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        rb_to_stable: dict[str, str] = {}
        # Intra-run dedupe: tier-3 collisions (empty path + mtime=0) mean
        # multiple RB rows share a stable_id. Honour the first and skip
        # subsequent collisions so reruns are truly idempotent.
        seen_sids: set[str] = set()

        for i, track in enumerate(_rb_rows(rb_db)):
            if limit is not None and i >= limit:
                break
            # Always feed the raw folder_path into the tier-3 hash so
            # streaming rows (spotify:/tidal:/http[s]:) collide only
            # when their URIs are identical. The previous behaviour
            # (empty path for streaming) meant every ISRC-less
            # streaming track hashed to sha1("|0.0"), so the
            # seen_sids guard below silently dropped all but the first.
            # See .planning/FAN-OUT-V2-TRIAGE-2026-04-17.md (2/3).
            path_str = track["folder_path"]
            mtime = 0.0
            # Only hit the filesystem for real local paths; skip
            # streaming URIs so we do not spuriously call
            # os.path.exists / os.path.getmtime on schemes that will
            # never resolve on disk.
            if (
                path_str
                and not track["is_streaming"]
                and os.path.exists(path_str)
            ):
                try:
                    mtime = os.path.getmtime(path_str)
                except OSError:
                    mtime = 0.0
            try:
                sid, tier = state_ids.stable_id(
                    isrc=track["isrc"],
                    fingerprint=None,
                    duration_ms=track["duration_ms"],
                    size_bytes=track["file_size"],
                    abs_path=path_str,
                    mtime=mtime,
                )
            except ValueError:
                report.tracks_skipped += 1
                continue

            rb_to_stable[track["id"]] = sid
            if sid in seen_sids:
                report.tracks_skipped += 1
                continue
            seen_sids.add(sid)

            changed = writer.upsert_track(
                stable_id=sid,
                stable_id_tier=tier,
                title=track["title"] or None,
                artists=[track["artist"]] if track["artist"] else [],
                album=track["album"] or None,
                isrc=track["isrc"],
                duration_ms=track["duration_ms"],
                file_path=path_str or None,
                content_hash=None,
            )
            if changed:
                existing_vendor = conn.execute(
                    "SELECT 1 FROM track_vendor_ids "
                    "WHERE stable_id = ? AND vendor = 'rekordbox'",
                    (sid,),
                ).fetchone()
                if existing_vendor is None:
                    report.tracks_inserted += 1
                else:
                    report.tracks_updated += 1
            else:
                report.tracks_unchanged += 1
            report.tier_counts[tier] = report.tier_counts.get(tier, 0) + 1

            writer.set_vendor_id(sid, "rekordbox", track["id"])

            modified_at = _rb_modified_at(track["updated_at"])
            if track["bpm"] is not None:
                if writer.set_field(
                    sid, "bpm", track["bpm"],
                    source="rekordbox", modified_at=modified_at,
                ):
                    report.fields_written += 1
            if track["key_name"]:
                if writer.set_field(
                    sid, "key", track["key_name"],
                    source="rekordbox", modified_at=modified_at,
                ):
                    report.fields_written += 1
            if track["rating"] is not None:
                if writer.set_field(
                    sid, "rating", track["rating"],
                    source="rekordbox", modified_at=modified_at,
                ):
                    report.fields_written += 1

        for rb_pl_id, pl_name, rb_tids in _rb_playlists(rb_db):
            pl_id = compute_playlist_id("rekordbox", rb_pl_id)
            inserted = writer.insert_playlist(
                playlist_id=pl_id, name=pl_name,
                vendor="rekordbox", vendor_pl_id=rb_pl_id,
            )
            if inserted:
                report.playlists_inserted += 1
            member_sids = [
                rb_to_stable[tid] for tid in rb_tids if tid in rb_to_stable
            ]
            writer.set_playlist_memberships(pl_id, member_sids)

        writer.register_adapter(
            "rekordbox",
            last_run_at=now_fn().isoformat(),
            last_ok=True,
            notes=(
                f"tracks={report.tracks_inserted + report.tracks_updated + report.tracks_unchanged} "
                f"dry_run={dry_run}"
            ),
        )

        if dry_run:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    finally:
        try:
            rb_db.close()
        except sqlite3.Error as exc:
            _log.warning("Failed to close Rekordbox DB", exc_info=exc)
        except Exception as exc:  # pragma: no cover - defensive cleanup path
            # Do not re-raise: a cleanup failure in the finally block must not
            # mask a successful ingest or shadow an in-flight exception from
            # the try body. Log loudly with a traceback so the failure is
            # visible to operators without breaking otherwise-successful runs.
            _log.error("Unexpected error closing Rekordbox DB", exc_info=exc)
        report.duration_s = round(time.perf_counter() - start, 3)

    return report


def _write_csv_artefact(
    conn: sqlite3.Connection, out_dir: Path, report: IngestReport
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"ingest-rekordbox-{stamp}.csv"
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "stable_id",
                "stable_id_tier",
                "vendor_id",
                "title",
                "artists_json",
                "isrc",
                "duration_ms",
                "file_path",
            ]
        )
        for row in conn.execute(
            "SELECT t.stable_id, t.stable_id_tier, v.vendor_id, t.title, "
            "t.artists_json, t.isrc, t.duration_ms, t.file_path "
            "FROM tracks t "
            "LEFT JOIN track_vendor_ids v "
            "ON v.stable_id = t.stable_id AND v.vendor = 'rekordbox'"
        ):
            w.writerow(row)
    return out_path


def _print_summary(report: IngestReport) -> None:
    print(f"Rekordbox ingest ({'dry-run' if report.dry_run else 'write'})")
    print(f"  source:           {report.rb_path}")
    print(f"  duration:         {report.duration_s:.3f}s")
    print(f"  tracks inserted:  {report.tracks_inserted}")
    print(f"  tracks updated:   {report.tracks_updated}")
    print(f"  tracks unchanged: {report.tracks_unchanged}")
    print(f"  tracks skipped:   {report.tracks_skipped}")
    for tier, n in report.tier_counts.items():
        print(f"  tier {tier:<12} {n}")
    print(f"  playlists new:    {report.playlists_inserted}")
    print(f"  fields written:   {report.fields_written}")


def _warn_if_stale(rb_path: Path) -> None:
    live = shared_paths.REKORDBOX_LIVE_DB
    if rb_path != shared_paths.REKORDBOX_WORKING_DB or not live.exists():
        return
    try:
        live_mtime = live.stat().st_mtime
        copy_mtime = rb_path.stat().st_mtime
    except OSError:
        return
    if live_mtime - copy_mtime > 60:
        print(
            f"warning: {rb_path} is older than {live}; "
            "rerun apps.audit.rekordbox_vs_music or pass --stale-ok to suppress",
            file=sys.stderr,
        )


def run_cli(args: argparse.Namespace) -> int:
    """Entry point called from ``apps.shared.state.cli ingest-rb``."""
    state_path = Path(args.db) if args.db else state_paths.STATE_DB
    rb_path = Path(args.rb_db) if args.rb_db else shared_paths.REKORDBOX_WORKING_DB
    if not rb_path.exists():
        print(f"error: Rekordbox DB not found at {rb_path}", file=sys.stderr)
        return 1
    if not args.stale_ok:
        _warn_if_stale(rb_path)

    try:
        conn = state_db.open_rw(state_path)
    except sqlite3.Error as exc:
        print(f"error opening state DB: {exc}", file=sys.stderr)
        return 1
    writer = StateWriter(conn, actor="ingest-rb")
    try:
        report = ingest_rb(
            writer,
            rb_path,
            dry_run=not args.write,
            limit=args.limit,
        )
        _print_summary(report)
        if args.write:
            csv_path = _write_csv_artefact(
                conn, shared_paths.STATE_DIR, report
            )
            print(f"  csv:              {csv_path}")
    except Exception as exc:  # pragma: no cover - full-traceback path
        import traceback

        traceback.print_exc()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        writer.close()
        conn.close()
    return 0


__all__ = ["IngestReport", "ingest_rb", "run_cli"]
