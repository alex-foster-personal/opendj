"""Materialisation engine (SMART-02)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from apps.smartlists.diff import diff_sets
from apps.smartlists.evaluator import evaluate
from apps.smartlists.repo import SmartlistsRepo
from apps.smartlists.writers import PlaylistWriter

DEFAULT_MARKER_PREFIX: str = "[SL] "


@dataclass(frozen=True)
class MaterializeResult:
    smartlist_id: str
    smartlist_name: str
    playlist_name: str
    added_tracks: list[str]
    removed_tracks: list[str]
    total_tracks: int
    writers_applied: dict[str, bool] = field(default_factory=dict)
    dry_run: bool = True
    errors: list[str] = field(default_factory=list)
    adopted_existing: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


class Materializer:
    """Evaluate -> diff -> push per smartlist.

    ``dry_run=True`` + ``live=False`` are the safe defaults; to actually
    push to writers the caller must set ``dry_run=False, live=True``
    (mirrors Phase 1 safety-rail pattern).

    Per-writer errors are captured in ``result.errors``; successful
    writers still get counted so a djay failure doesn't silently hide a
    successful RB write. When any writer fails the materialisation
    state (``last_materialized_track_ids``) is NOT persisted so a retry
    can replay the same diff.
    """

    def __init__(
        self,
        sm_repo: SmartlistsRepo,
        writers: Iterable[PlaylistWriter] = (),
        *,
        marker_prefix: str = DEFAULT_MARKER_PREFIX,
    ) -> None:
        self.sm_repo = sm_repo
        self.writers: list[PlaylistWriter] = list(writers)
        self.marker_prefix = marker_prefix

    def _playlist_name(self, smartlist_name: str) -> str:
        return f"{self.marker_prefix}{smartlist_name}"

    def materialize(
        self,
        smartlist_id: str,
        *,
        dry_run: bool = True,
        live: bool = False,
        force_adopt: bool = False,
    ) -> MaterializeResult:
        row = self.sm_repo.get_by_id(smartlist_id)
        if row is None:
            raise ValueError(f"smartlist {smartlist_id!r} not found")
        new_ids = evaluate(
            row.rule, self.sm_repo.conn,
            order_by=row.order_by, validate=False,
        )
        old_ids = row.last_materialized_track_ids
        added, removed = diff_sets(old_ids, new_ids)
        playlist_name = self._playlist_name(row.name)

        errors: list[str] = []
        writers_applied: dict[str, bool] = {}
        adopted_existing = False

        if not dry_run and live:
            for writer in self.writers:
                try:
                    exists = writer.playlist_exists(playlist_name)
                    if exists and not old_ids and not force_adopt:
                        raise RuntimeError(
                            f"{writer.vendor}: playlist {playlist_name!r} "
                            "already exists; pass force_adopt=True to overwrite"
                        )
                    if exists:
                        writer.apply_diff(playlist_name, added, removed)
                        adopted_existing = adopted_existing or (not old_ids)
                    else:
                        writer.create_playlist(playlist_name, new_ids)
                    writers_applied[writer.vendor] = True
                except Exception as exc:
                    writers_applied[writer.vendor] = False
                    errors.append(f"{writer.vendor}: {exc}")
            if any(writers_applied.values()) and not errors:
                self.sm_repo.mark_materialized(row.id, new_ids)

        return MaterializeResult(
            smartlist_id=row.id,
            smartlist_name=row.name,
            playlist_name=playlist_name,
            added_tracks=added,
            removed_tracks=removed,
            total_tracks=len(new_ids),
            writers_applied=writers_applied,
            dry_run=dry_run or not live,
            errors=errors,
            adopted_existing=adopted_existing,
        )

    def materialize_all(
        self,
        *,
        dry_run: bool = True,
        live: bool = False,
        force_adopt: bool = False,
    ) -> list[MaterializeResult]:
        results: list[MaterializeResult] = []
        for row in self.sm_repo.list_all():
            results.append(
                self.materialize(
                    row.id, dry_run=dry_run, live=live,
                    force_adopt=force_adopt,
                )
            )
        return results


__all__ = ["DEFAULT_MARKER_PREFIX", "MaterializeResult", "Materializer"]
