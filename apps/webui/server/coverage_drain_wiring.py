"""Build the engine's coverage drain from an app: real jobs, real gates.

Split out of ``coverage_drain`` (file-size ceiling). ``coverage_drain
.build_for_app`` forwards here, so the lifespan keeps one entry point.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from apps.lyrics.service import LyricsFetchService
from apps.shared.events import publish
from apps.webui.server import boot_grace, coverage_memory, coverage_recency, stem_cache_enforcer
from apps.webui.server import coverage_cloud_vocals as cloud_vocals_mod
from apps.webui.server import coverage_drain_analysis as analysis_step
from apps.webui.server import coverage_outcomes as outcomes_mod
from apps.webui.server import coverage_stems_terminal as stems_terminal
from apps.webui.server.coverage_drain import CoverageDrain
from apps.webui.server.coverage_drain_analysis import AnalysisPolicy, DeckGate, analysis_job
from apps.webui.server.coverage_drain_jobs import (
    lyrics_job,
    vocals_capability_refusal,
    vocals_job,
)
from apps.webui.server.coverage_drain_state import DrainConfig, config_path


def build_for_app(app: FastAPI) -> CoverageDrain:
    """The engine's drain: real jobs, the app's own snapshot and deck mirror."""
    from apps.sets.paths import SETS_DB
    from apps.webui.server.routes import ingest as ingest_routes

    data_dir = ingest_routes.COVERAGE_DATA_DIR
    refusals = {
        "vocals": vocals_capability_refusal(),
        "analysis": analysis_step.analysis_capability_refusal(),
    }

    def mirror() -> Any:
        return getattr(app.state, "ui_mirror", None)

    def changed(step: str, stable_id: str) -> None:
        # ``library_jobs`` is the kind the user-ordered stems/lyrics lanes
        # already publish for a finished per-track artifact.
        _ = step
        publish("library.changed", {"kind": "library_jobs", "ids": [stable_id]})

    def cloud_inputs() -> cloud_vocals_mod.CloudInputs:
        # The evictor's own view of the cache, so the two cannot disagree
        # about which directory, index or open decks they mean.
        inputs = stem_cache_enforcer.cache_inputs(app)
        return cloud_vocals_mod.CloudInputs(
            stems_dir=inputs.stems_dir,
            index=inputs.index,
            protected=inputs.protected,
            source=getattr(app.state, "stem_hydration_source", None),
        )

    def title_of(stable_id: str) -> str | None:
        conn = ingest_routes.open_ro()
        try:
            row = conn.execute(
                "SELECT title FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
                (stable_id,),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else row[0]

    outcomes = outcomes_mod.OutcomeStore(outcomes_mod.store_path(data_dir))
    config = DrainConfig(config_path(data_dir))
    derive_vocals = vocals_job(data_dir, ingest_routes._stem_roots(app))
    drain = CoverageDrain(
        snapshot_fn=lambda: ingest_routes.build_snapshot(app),
        jobs={
            "vocals": derive_vocals,
            "lyrics": lyrics_job(LyricsFetchService(data_dir)),
            "analysis": analysis_job(data_dir, backend=ingest_routes.ANALYSIS_BACKEND),
        },
        # Playing OR loading: see ``DeckGate``.
        playing_fn=DeckGate(mirror),
        outcomes=outcomes,
        config=config,
        unavailable_steps={step: why for step, why in refusals.items() if why is not None},
        on_change=changed,
        boot_grace=boot_grace.for_app(app),
        cloud_vocals=cloud_vocals_mod.CloudVocals(
            data_dir=data_dir,
            inputs_fn=cloud_inputs,
            cap_fn=config.transient_bundle_cap,
            derive=derive_vocals,
        ),
        stems_check=stems_terminal.StemsCheck(
            outcomes=outcomes,
            keep=stems_terminal.KeepPending(stems_terminal.keep_pending_path(data_dir)),
            title_fn=title_of,
        ),
    )
    drain.analysis_policy = AnalysisPolicy(
        user_jobs_fn=lambda: analysis_step.user_jobs_active(
            ingest_routes.open_ro, lambda: ingest_routes.refresh_status().running
        ),
        memory_pressure_fn=coverage_memory.pressure_reason,
        recency_fn=lambda: coverage_recency.recent_track_ids(SETS_DB, mirror()),
        farm_only_stages=analysis_step.FARM_ONLY_STAGES,
        farm_pending_fn=lambda present: analysis_step.farm_only_pending(
            ingest_routes.open_ro, present
        ),
    )
    return drain


__all__ = ["build_for_app"]
