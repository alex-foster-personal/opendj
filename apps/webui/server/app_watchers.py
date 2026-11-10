"""Auto-analyze and lyric-index watcher wiring for the webui daemon."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from . import analysis_autostart, lyric_index_autostart
from .routes import ingest as ingest_routes
from .routes import ui_prefs


def build_auto_analyze_watcher(app: FastAPI) -> analysis_autostart.AutoAnalyzeWatcher:
    """Bind the reconcile loop to the one-slot ingest refresh job.

    Public because it IS the wiring under test: a test that re-declares these
    three callables would keep passing after the daemon started draining the
    wrong scope. Call this instead.
    """

    def start_unmapped_drain(
        guard: analysis_autostart.GuardFn,
    ) -> analysis_autostart.ConsumedFn:
        """Start the drain and hand back a reader bound to THAT job.

        ``guard`` is the watcher's own refusal, and it is handed DOWN to the
        locked start rather than run here, so the ledger read it judges and
        the slot claim happen inside one hold of the registry lock. Run here
        instead and there is a moment between them again - short, but a
        parked thread is parked for as long as the scheduler says, and a
        drain that finishes in it makes this start a repeat of one that
        already attempted the same queue.

        The job object comes back FROM the locked start, so it is the one
        the registry just created and the reader survives a manual refresh
        claiming the single slot. Reread off the registry instead and the
        window is one statement wide - a short drain can finish and a library
        refresh claim the slot in it - and a library job, which publishes no
        queue signature, would then erase the booking.

        The signature is reached through the job rather than
        RefreshStatusOut because it is internal reconcile state, not part of
        the wire contract the browser polls: putting it on the response model
        would drift the OpenAPI schema for a value no client can use.
        """
        started = ingest_routes._start_refresh_job(
            ingest_routes.RefreshIn(scope="unmapped"),
            ingest_routes._stem_roots(app),
            lambda: guard(last_attempted_queue()),
            stem_cloud=ingest_routes.stem_cloud_for(app),
            auto_lanes=ui_prefs.read_auto_analyze_lanes(app),
        )

        def consumed() -> str | None:
            # The LATEST FINISHED unmapped drain, which is not necessarily
            # ours: a manual POST /analysis-queue/run targets the same derived
            # queue, and the queue IT just failed on is the one worth
            # suppressing.
            #
            # Read off `last_unmapped` rather than the one slot, because the
            # slot is replaceable and a library or batch refresh landing after
            # that drain hides it completely. Its signature would then be read
            # as a silence and OUR older one booked instead - and when the
            # newer drain had deliberately CLEARED its signature, because the
            # CLI reported a target it never admitted, that older booking is
            # exactly the suppression the clearing existed to prevent. The
            # lost target then sits behind `unchanged` for as long as it keeps
            # its content token.
            latest = ingest_routes._JOBS.last_unmapped
            if latest is not None and latest.phase not in ingest_routes.ACTIVE_PHASES:
                return latest.queue_signature
            # No unmapped drain has finished in this process, or the newest
            # one is still running and its signature is not final. Fall back
            # to what OUR drain read rather than booking None, which would
            # drop the suppression entirely.
            return started.queue_signature

        return consumed

    def last_attempted_queue() -> analysis_autostart.Attempt | None:
        """The newest FINISHED drain, whoever started it.

        The watcher only ever hears about its OWN drains through
        ``start_unmapped_drain``. A manual POST /analysis-queue/run targets
        the same derived queue and is invisible to it, which is the hole this
        closes.

        The job object goes back with the signature so the watcher can tell a
        NEW attempt from the same finished job still sitting in the one-slot
        registry, and the SCOPE goes with it because the watcher cannot read
        a `None` signature without knowing whether the job was ever about this
        backlog. Identity and scope are the caller's business; only the
        signature is interpreted here.

        FINISHED is load-bearing and is checked, not assumed. A manual drain
        that claims the slot after ``running_fn`` said no publishes its
        signature EARLY - ``_unmapped_targets`` writes it before the first
        chunk runs - so a still-running job is visible here with a signature
        that is not its final one. Adopting it would book a queue whose verdict
        is not in yet, and the case that costs is the one the publish protocol
        exists for: a CLI that then reports a vanished target clears
        ``queue_signature`` precisely so the queue is NOT booked, and the
        booking would already have happened. Restoring that file recreates the
        same signature, the loop reports ``unchanged`` forever, and a track
        nothing ever attempted is never analyzed.
        """
        current = ingest_routes._JOBS.current
        if current is not None and current.scope == ingest_routes.UNMAPPED_SCOPE:
            if current.phase in ingest_routes.ACTIVE_PHASES:
                return None
            return analysis_autostart.Attempt(
                job=current,
                signature=current.queue_signature,
                targets_backlog=True,
            )
        # The slot holds a library or batch job, or nothing at all, and so
        # says nothing about this backlog. A finished unmapped drain it
        # REPLACED still does, and that statement has to outlive the slot for
        # the same reason `consumed` reads it: a drain that cleared its
        # signature is asking for its queue to be retried, and a library
        # refresh landing after it must not be what silences that.
        latest = ingest_routes._JOBS.last_unmapped
        if latest is not None and latest.phase not in ingest_routes.ACTIVE_PHASES:
            return analysis_autostart.Attempt(
                job=latest,
                signature=latest.queue_signature,
                targets_backlog=True,
            )
        if current is None or current.phase in ingest_routes.ACTIVE_PHASES:
            return None
        return analysis_autostart.Attempt(
            job=current,
            signature=current.queue_signature,
            targets_backlog=False,
        )

    return analysis_autostart.AutoAnalyzeWatcher(
        app.state.auto_analyze,
        scan_fn=lambda: ingest_routes.unmapped_backlog(
            lanes=ui_prefs.read_auto_analyze_lanes(app)
        ),
        start_fn=start_unmapped_drain,
        running_fn=lambda: ingest_routes.refresh_status().running,
        attempted_fn=last_attempted_queue,
    )


def build_lyric_index_watcher(app: FastAPI) -> lyric_index_autostart.LyricIndexWatcher:
    """Bind the lyric-index reconcile loop to this app's data and usage store.

    Public because it IS the wiring under test: ``create_app`` supplies
    ``state_db_path`` (whose grandparent directory is the data root that
    holds ``state/`` - the lyrics cache and the index file both live under
    ``data_dir/state/``) and ``usage_store`` (the UI-activity probe that
    pauses the job while someone is using the app).
    """
    state_db_path = Path(app.state.state_db_path).resolve()
    data_dir = state_db_path.parent.parent
    # A layout mismatch (state_db_path not exactly two levels under the data
    # root) makes the derivation above point at the wrong tree: the watcher
    # would then find zero candidates, write the index into that wrong tree,
    # and report idle forever with no error. Fail loud instead - but only
    # when the daemon is actually armed to do that reconcile work, so an app
    # built with the feature off (the default in every test but the lyric
    # daemon's own) is free to pass a state_db_path with no such layout.
    if app.state.lyric_index.enabled and (
        state_db_path.parent.name != "state" or not (data_dir / "state").is_dir()
    ):
        raise RuntimeError(
            f"lyric index data_dir {data_dir} guessed from state_db_path "
            f"{state_db_path} does not match the required data_dir/state/"
            "state.db layout; refusing to reconcile a directory that cannot "
            "hold the lyrics cache."
        )

    def ui_active() -> bool:
        # "is the app open and on screen" is the DJ-safe pause signal: index
        # work never runs while a client could be at the decks.
        return bool(
            app.state.usage_store.snapshot()["summary"]["any_client_in_use"]
        )

    return lyric_index_autostart.LyricIndexWatcher(
        app.state.lyric_index, data_dir=data_dir, activity_fn=ui_active
    )
