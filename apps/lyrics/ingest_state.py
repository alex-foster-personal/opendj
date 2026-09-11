"""Load a finished bench run into state.db: verdicts + the words artifact.

`python -m apps.lyrics ingest-state --manifest FILE [--coverage FILE]
[--match-by vendor-id|file-path] [--write]`.

The bench manifest already joins jobs, predictions and witness verdicts, so
this reads THAT rather than re-deriving anything: one lineage from aligner to
UI. Its own module (not ``__main__``) because the PR-3 batch driver imports
it.

Two inputs, both files:
  * the manifest ``scripts/bench/lyrics_{corpus}_manifest.json``, read as
    ``payload["tracks"]`` when it is a dict, else the list itself;
  * an optional ``vocal-presence.json``, a list of
    ``{track_id, coverage_pct}`` that supplies the coverage the verdict bands.

Tracks are matched to state.db by their rekordbox vendor id (default) or, for
corpora joined by file path, by ``tracks.file_path`` and ONLY when exactly one
row holds it. Unmatched tracks are reported, never silently dropped, because a
shrinking denominator is how a coverage claim goes wrong (house rule).

Per-track failures do not abort the run: they are collected, printed with the
reason, and make the exit code non-zero, so a 200-track batch neither hides a
bad track nor loses the other 199.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.cloud import asset_store
from apps.cloud.config import CloudConfig
from apps.lyrics import store
from apps.lyrics.artifacts import produce_words_artifact
from apps.lyrics.karaoke_cache import PIPELINE_VERSION
from apps.lyrics.vocal_presence import coverage_verdict
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp

MATCH_MODES: tuple[str, ...] = ("vendor-id", "file-path")


@dataclass(frozen=True)
class IngestReport:
    """Honest counts over the manifest's own denominator."""

    n_tracks: int
    n_matched: int
    n_ingested: int
    n_words: int
    unmatched: tuple[str, ...]
    failures: tuple[str, ...]


#-----------------------------------------------------------------------------
# matching
#-----------------------------------------------------------------------------
def _match_vendor_id(
    conn: sqlite3.Connection, track: dict[str, Any]
) -> tuple[str | None, str]:
    row = conn.execute(
        "SELECT stable_id FROM track_vendor_ids "
        "WHERE vendor='rekordbox' AND vendor_id=?",
        (track["name"],),
    ).fetchone()
    if row is None:
        return None, "no rekordbox vendor id in state.db"
    return str(row[0]), ""


def _match_file_path(
    conn: sqlite3.Connection, track: dict[str, Any]
) -> tuple[str | None, str]:
    if not track.get("file_path"):
        return None, "no file_path key in manifest entry"
    rows = conn.execute(
        "SELECT stable_id FROM tracks WHERE file_path=? AND deleted_at IS NULL",
        (track["file_path"],),
    ).fetchall()
    ids = sorted({str(row[0]) for row in rows})
    if len(ids) == 1:
        return ids[0], ""
    return None, f"{len(ids)} rows hold this file_path"


def _match_tracks(
    conn: sqlite3.Connection, tracks: Sequence[dict[str, Any]], match_by: str
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """(matched pairs, unmatched labels). Never guesses; never drops silently."""
    if match_by not in MATCH_MODES:
        raise ValueError(f"unhandled match_by {match_by!r}, not in {list(MATCH_MODES)}")
    matched: list[tuple[str, dict[str, Any]]] = []
    unmatched: list[str] = []
    for track in tracks:
        label = f"{track['name']} ({track.get('artist', '?')} - {track.get('title', '?')})"
        if match_by == "vendor-id":
            stable_id, reason = _match_vendor_id(conn, track)
        elif match_by == "file-path":
            stable_id, reason = _match_file_path(conn, track)
        else:
            raise AssertionError(f"unhandled match_by {match_by!r}")
        if stable_id is None:
            unmatched.append(f"{label} [{reason}]")
        else:
            matched.append((stable_id, track))
    return matched, unmatched


#-----------------------------------------------------------------------------
# the write half
#-----------------------------------------------------------------------------
def _ingest_one(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    stable_id: str,
    track: dict[str, Any],
    coverage_pct: float | None,
    s3: asset_store.AssetS3Client | None,
    cfg: CloudConfig | None,
) -> int:
    """Write one track's artifact + verdict row. Returns words written."""
    stats = track.get("stats", {})
    source = stats.get("source_method")
    if not source:
        raise ValueError(
            f"{stable_id}: manifest entry has no stats.source_method, so the "
            "artifact could not name its provenance and purge could never "
            "find it by source"
        )
    artifact = produce_words_artifact(
        conn,
        data_dir=data_dir,
        stable_id=stable_id,
        source=source,
        words=track["words"],
        s3=s3,
        cfg=cfg,
    )
    claimed = track.get("n_words")
    if claimed is not None and int(claimed) != artifact.n_words:
        raise ValueError(
            f"{stable_id}: manifest claims n_words={claimed} but the written "
            f"artifact holds {artifact.n_words}; two sources disagree about "
            "the same track, so neither is trusted"
        )
    store.upsert_verdict(
        conn,
        stable_id=stable_id,
        verdict=coverage_verdict(coverage_pct) if coverage_pct is not None else "unknown",
        coverage_pct=coverage_pct,
        source=source,
        language_iso3=track.get("language_iso"),
        n_words=artifact.n_words,
        n_lines=artifact.n_lines,
        pct_witness_red=stats.get("pct_witness_red"),
        pipeline_version=PIPELINE_VERSION,
        words_content_hash=artifact.content_hash,
        computed_at=sync_stamp.canonical_now(),
        resurrect=False,
    )
    return artifact.n_words


def _load_inputs(
    manifest: Path, coverage: Path | None
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    tracks = payload["tracks"] if isinstance(payload, dict) else payload
    coverage_by_id: dict[str, float] = {}
    if coverage is not None:
        coverage_by_id = {
            row["track_id"]: row["coverage_pct"]
            for row in json.loads(coverage.read_text(encoding="utf-8"))
        }
    return list(tracks), coverage_by_id


def ingest_state(
    *,
    manifest: Path,
    coverage: Path | None,
    write: bool,
    match_by: str,
    db_path: Path | None,
    s3: asset_store.AssetS3Client | None,
    cfg: CloudConfig | None,
) -> IngestReport:
    """Ingest a bench run. ``write=False`` matches and reports, writing nothing."""
    tracks, coverage_by_id = _load_inputs(manifest, coverage)
    conn = state_db.open_rw(db_path) if write else state_db.open_ro(db_path)
    try:
        data_dir = sync_stamp.data_dir_for_connection(conn)
        matched, unmatched = _match_tracks(conn, tracks, match_by)
        n_words = 0
        n_ingested = 0
        failures: list[str] = []
        for stable_id, track in matched:
            if not write:
                continue
            try:
                n_words += _ingest_one(
                    conn,
                    data_dir=data_dir,
                    stable_id=stable_id,
                    track=track,
                    coverage_pct=coverage_by_id.get(track["name"]),
                    s3=s3,
                    cfg=cfg,
                )
            except (ValueError, KeyError, RuntimeError) as error:
                failures.append(f"{track['name']} -> {stable_id}: {error}")
            else:
                n_ingested += 1
    finally:
        conn.close()
    return IngestReport(
        n_tracks=len(tracks),
        n_matched=len(matched),
        n_ingested=n_ingested,
        n_words=n_words,
        unmatched=tuple(unmatched),
        failures=tuple(failures),
    )


def format_report(report: IngestReport, *, manifest: Path, write: bool) -> str:
    """The operator-facing summary, always naming its denominator."""
    head = "OK" if write else "DRY-RUN"
    body = [
        f"[{head}] {report.n_matched} tracks matched to state.db, "
        f"{report.n_ingested} ingested, {report.n_words} words written "
        f"(denominator: {report.n_tracks} tracks in {manifest.name})"
    ]
    if report.unmatched:
        body.append(
            f"[WARN] {len(report.unmatched)} track(s) could not be matched to "
            "this library snapshot:"
        )
        body.extend(f"       {label}" for label in report.unmatched[:10])
    if report.failures:
        body.append(f"[ERROR] {len(report.failures)} track(s) failed to ingest:")
        body.extend(f"        {failure}" for failure in report.failures[:10])
    if not write:
        body.append("[..] nothing written; re-run with --write")
    return "\n".join(body)


def main(
    *,
    manifest: Path,
    coverage: Path | None,
    write: bool,
    match_by: str,
    db_path: Path | None,
    s3: asset_store.AssetS3Client | None,
    cfg: CloudConfig | None,
) -> int:
    """CLI entry point: run, print, and exit non-zero if any track failed."""
    report = ingest_state(
        manifest=manifest,
        coverage=coverage,
        write=write,
        match_by=match_by,
        db_path=db_path,
        s3=s3,
        cfg=cfg,
    )
    print(format_report(report, manifest=manifest, write=write))
    return 1 if report.failures else 0


__all__ = [
    "MATCH_MODES",
    "IngestReport",
    "format_report",
    "ingest_state",
    "main",
]
