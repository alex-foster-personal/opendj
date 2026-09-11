"""The licensing lever: remove one provider's lyrics from every store.

`python -m apps.lyrics purge --source <prefix> [--dry-run]`.

Licensed lyric text lives in three places at once: the ``lyric_verdict`` row,
the local ``karaoke_words`` artifact, and (in cloud mode) the
content-addressed object in R2. A purge that clears one of them is not a
purge, so this reports honest per-store counts and never infers one from
another.

The row is TOMBSTONED, never hard-deleted: the tombstone is what syncs, so
peers stop hydrating instead of pushing the rows back. Content addressing
means the same words produced on any peer share one key, so deleting that key
removes the text for every machine at once.

One ``stamped_transaction`` per row (each row is its own changelog entry), so
an interrupted purge leaves a coherent prefix of tombstoned rows rather than a
half-written batch, and the re-run finishes the job.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from apps.cloud import asset_store, policy
from apps.cloud.config import CloudConfig
from apps.lyrics import karaoke_cache, store
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp

DRY_RUN_R2_REASON: str = (
    "dry run: no R2 delete attempted, so no object count can be claimed"
)
LOCAL_MODE_R2_REASON: str = (
    "cloudsync mode is 'local': this machine has no R2 credentials and pushed "
    "nothing, so there is no object to delete"
)


@dataclass(frozen=True)
class PurgeReport:
    """What the purge did, per store. Every count is observed, never inferred."""

    source_prefix: str
    dry_run: bool
    rows_matched: int
    rows_tombstoned: int
    files_removed: int
    files_absent: int
    objects_deleted: int
    objects_absent: int
    r2_skipped_reason: str | None
    stable_ids: tuple[str, ...]


@dataclass
class _Counts:
    files_removed: int = 0
    files_absent: int = 0
    objects_deleted: int = 0
    objects_absent: int = 0


def _remove_file(path: Path, dry_run: bool, counts: _Counts) -> None:
    if not path.is_file():
        counts.files_absent += 1
        return
    counts.files_removed += 1
    if not dry_run:
        path.unlink()


def _delete_object(
    content_hash: str,
    s3: asset_store.AssetS3Client,
    cfg: CloudConfig,
    counts: _Counts,
) -> None:
    if asset_store.delete_asset(cfg, s3, content_hash):
        counts.objects_deleted += 1
    else:
        counts.objects_absent += 1


def _r2_skip_reason(dry_run: bool) -> str | None:
    """Why R2 was left alone, or None when it was actually visited."""
    if policy.CFG.mode == "local":
        return LOCAL_MODE_R2_REASON
    elif policy.CFG.mode == "cloud":  # noqa: RET505 - explicit elif is the house style
        return DRY_RUN_R2_REASON if dry_run else None
    else:
        raise AssertionError(f"unhandled cloudsync mode {policy.CFG.mode!r}")


def purge_by_source(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    source_prefix: str,
    s3: asset_store.AssetS3Client | None,
    cfg: CloudConfig | None,
    dry_run: bool,
) -> PurgeReport:
    """Purge every live verdict whose ``source`` starts with ``source_prefix``."""
    if not source_prefix:
        raise store.LyricStoreError(
            "purge needs a non-empty --source prefix; an empty prefix would "
            "match the whole library."
        )
    verdicts = store.verdicts_by_source(conn, source_prefix)
    skip_reason = _r2_skip_reason(dry_run)
    clients: tuple[asset_store.AssetS3Client, CloudConfig] | None = None
    if skip_reason is None:
        if s3 is None or cfg is None:
            raise store.LyricStoreError(
                "cloudsync mode is 'cloud' but no S3 client / CloudConfig was "
                "supplied; a purge that cannot reach R2 would leave the "
                "licensed text in the bucket."
            )
        clients = (s3, cfg)
    counts = _Counts()
    tombstoned = 0
    for verdict in verdicts:
        _remove_file(
            karaoke_cache.cache_path(data_dir, verdict.stable_id), dry_run, counts
        )
        if clients is not None and verdict.words_content_hash is not None:
            _delete_object(verdict.words_content_hash, clients[0], clients[1], counts)
        if not dry_run:
            store.tombstone(conn, verdict.stable_id)
            tombstoned += 1
    return PurgeReport(
        source_prefix=source_prefix,
        dry_run=dry_run,
        rows_matched=len(verdicts),
        rows_tombstoned=tombstoned,
        files_removed=counts.files_removed,
        files_absent=counts.files_absent,
        objects_deleted=counts.objects_deleted,
        objects_absent=counts.objects_absent,
        r2_skipped_reason=skip_reason,
        stable_ids=tuple(verdict.stable_id for verdict in verdicts),
    )


def format_report(report: PurgeReport) -> str:
    """The operator-facing summary the CLI prints."""
    head = "DRY-RUN" if report.dry_run else "OK"
    body = (
        f"[{head}] purge source LIKE {report.source_prefix!r}%: "
        f"{report.rows_matched} live rows matched, "
        f"{report.rows_tombstoned} tombstoned, "
        f"{report.files_removed} local artifacts removed "
        f"({report.files_absent} already absent), "
        f"{report.objects_deleted} R2 objects deleted "
        f"({report.objects_absent} already absent)"
    )
    if report.r2_skipped_reason is not None:
        body += f"\n[WARN] R2 untouched: {report.r2_skipped_reason}"
    return body


def main(
    *,
    source_prefix: str,
    db_path: Path | None,
    dry_run: bool,
    s3: asset_store.AssetS3Client | None,
    cfg: CloudConfig | None,
) -> int:
    """CLI entry point: purge, print the per-store counts, exit 0."""
    conn = state_db.open_rw(db_path)
    try:
        report = purge_by_source(
            conn,
            data_dir=sync_stamp.data_dir_for_connection(conn),
            source_prefix=source_prefix,
            s3=s3,
            cfg=cfg,
            dry_run=dry_run,
        )
    finally:
        conn.close()
    print(format_report(report))
    return 0


__all__ = [
    "DRY_RUN_R2_REASON",
    "LOCAL_MODE_R2_REASON",
    "PurgeReport",
    "format_report",
    "main",
    "purge_by_source",
]
