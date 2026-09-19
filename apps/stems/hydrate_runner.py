"""Shared indexed stem hydration runner for job subprocesses (issue #2630)."""
from __future__ import annotations

import json
from pathlib import Path

from apps.cloud import stem_hydration, stem_index
from apps.cloud.stem_source import StemSourceError, resolve_stem_hydration_source
from apps.stems.job import PROGRESS_TRACK_KEY


class HydrateRunnerError(RuntimeError):
    """One or more tracks failed hydration."""


def _emit(progress: float, message: str, **extra: object) -> None:
    line = json.dumps(
        {"progress": round(float(progress), 4), "message": message, **extra},
        separators=(",", ":"),
    )
    print(line, flush=True)


def hydrate_stable_ids(
    stable_ids: list[str],
    data_dir: Path,
    *,
    refresh_index: bool = True,
) -> None:
    """Hydrate ``stable_ids`` through the production stem hydration path."""
    source = resolve_stem_hydration_source(data_dir)
    if source is None:
        raise HydrateRunnerError(
            "no stem hydration source (need R2 credentials or a configured hub)"
        )
    if refresh_index:
        source.refresh_index(data_dir, force=True)
    index = stem_index.load_cached_index(data_dir)
    stems_dir = data_dir / "state" / "stems"
    failures: list[str] = []
    total = len(stable_ids)
    for offset, stable_id in enumerate(stable_ids, start=1):
        if stable_id not in index:
            failures.append(f"{stable_id} (not indexed)")
            continue
        try:
            outcome = stem_hydration.hydrate_one(
                stable_id,
                data_dir=data_dir,
                source=source,
                index=index,
                stems_dir=stems_dir,
                skip_reserved=False,
            )
        except StemSourceError as exc:
            failures.append(f"{stable_id} ({exc.code}: {exc.message})")
            continue
        if outcome.status in ("hydrated", "already_local"):
            _emit(
                offset / total,
                f"hydrated {stable_id}",
                **{PROGRESS_TRACK_KEY: stable_id},
            )
        else:
            failures.append(
                f"{stable_id} ({outcome.status}: {outcome.reason or 'failed'})"
            )
    if failures:
        raise HydrateRunnerError(
            "hydration failed for "
            f"{len(failures)} track(s): {'; '.join(failures[:10])}"
        )


__all__ = ["HydrateRunnerError", "hydrate_stable_ids"]
