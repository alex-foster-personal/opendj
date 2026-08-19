"""The ``stems.separate`` engine job kind: Modal separation, one job, N tracks.

WHAT THIS IS FOR. A tester installs the app and wants stems. Separation is a
GPU job, the GPU is rented per second on Modal, and the tracks land one at a
time over several minutes. So the shape is: enqueue every track in one job,
watch one progress bar, and let each track's stems appear in the library the
moment ITS bundle is on disk -- not when the last track finishes.

Three seams make that work, and none of them is new machinery:

* :func:`build_argv` -- the argv builder. The worker is a SUBPROCESS, and it
  has to be, because it needs ``modal`` and torch-adjacent imports that are
  deliberately not in the repo venv (see ``scripts/stems_modal_worker.py``).
  So the argv is a ``uv run --with modal`` overlay, not ``sys.executable``.
* :func:`on_progress` -- how a per-track completion becomes a
  ``library.changed`` event. The worker names the finished track in its
  progress line; the engine turns that into the event the browser already
  subscribes to. A subprocess cannot publish to an in-process hub, so that
  translation has to happen engine-side.
* :func:`reconcile_from_disk` -- what to believe about a job the engine lost.
  This kind can often answer honestly, because the bundles on disk ARE the
  record.

THIS MODULE DOES NOT REGISTER ITSELF, and imports nothing from
``apps.engine_core``. The engine's ``create_app`` is the composition root and
wires the three functions above into the job registry. Registering here would
make the domain package import the chassis that imports it back -- a package
cycle the architecture gate fails on, and a stems job that could not be read
or tested without booting a server.

Requirements (mini-PRD):
  ✔︎ ✅ enqueue takes stable ids and nothing else is required.
    [if] payload has no ``stable_ids`` [then ⛔️] the enqueue is refused
    [if] an id is not a safe stable id [then ⛔️] refused before any spend
    [if] the same id appears twice [then] it is separated once
  ✔︎ ✅ every completed track publishes ``library.changed`` while the job is
    still running, so the library lights up progressively.
    [if] the worker reports track 3 done [then] one event names track 3
    [if] a line reports no track [then] no event is published
  ✔︎ ✅ a lost job is reconciled from the artifacts, never guessed.
    [if] every requested bundle is on disk [then] succeeded
    [if] some bundle is missing [then] failed, and re-enqueue is allowed

-Claude
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from apps.shared import events
from apps.shared.stable_id import is_safe_stable_id_segment
from apps.stems.selection import has_bundle
from apps.stems.tiers import DEFAULT_TIER, modal_tiers

# ----- CFG -------------------------------------------------------------------
JOB_KIND: str = "stems.separate"
"""Dotted on purpose: ``<domain>.<verb>``, so a kinds list stays readable once
there is more than one and a UI can group by the part before the dot."""

WORKER_SCRIPT: str = "scripts/stems_modal_worker.py"

SCOPE_PENDING: str = "pending"
"""The only scope: every library row with audio on disk and no bundle yet.

One scope, not a query language. The install flow asks one question and this
is its answer; a second scope should arrive with a second caller that needs
it, not in advance.
"""

MODAL_TIER_KEYS: tuple[str, ...] = tuple(tier.key for tier in modal_tiers())
"""The rungs this kind will run. Read from tiers.py so the ladder has ONE
home: adding a rung there makes it enqueueable here with no edit."""

LIBRARY_KIND: str = "tracks"
"""``library.changed`` kind for a landed bundle.

NOT a new ``stems`` kind, deliberately. What changed IS a set of track rows:
every listing payload carries a ``stems`` summary inline
(``PlaylistTrackRowWire.stems``), so the browser's existing ``tracks``
subscription refetches and the V/I/D chips light up with no new frontend
contract at all. Inventing a kind nothing consumes would announce the change
to nobody -- ``apps/webui/frontend/src/lib/api/events-bus.ts`` drops kinds it
does not know.
"""

PROGRESS_TRACK_KEY: str = "stems_ready"
"""The worker's own key, riding alongside ``progress`` on a progress line.

The runner's contract is ``{"progress": <number>, "message": <string|null>}``
and extra keys are passed through untouched, which is exactly the room a
batch kind needs to say WHICH item finished.
"""

# uv, not sys.executable: modal is not a repo dependency and must not become
# one (heavy ML deps never enter the repo venv -- CLAUDE.md). --with overlays
# it into an ephemeral env for this process only.
UV_BIN: str = os.environ.get("MDT_UV_BIN", "uv")


class StemsJobPayloadError(ValueError):
    """The enqueue payload cannot describe a run, so no run is started."""


# ----- payload ---------------------------------------------------------------


def parse_payload(payload: dict[str, Any]) -> tuple[list[str] | None, str, Path | None]:
    """Validate an enqueue payload into (stable_ids, tier, data_dir).

    ``stable_ids`` comes back None for ``scope: "pending"`` -- the caller
    asked for "everything that still needs stems" and the answer is whatever
    that is AT RUN TIME, resolved once in the worker. Freezing the list here
    would let a scan that finishes between enqueue and spawn go unseparated,
    and the install flow enqueues while the first scan is still running.

    Raised errors surface as a 400 from ``POST /api/v1/jobs`` because
    ``worker_argv`` runs BEFORE the row is inserted. That ordering is what
    makes a typo cost nothing: a bad payload never becomes a queued job, and
    a queued job never becomes GPU spend.
    """
    return (
        _parse_target(payload),
        _parse_tier(payload),
        _parse_data_dir(payload),
    )


def _parse_target(payload: dict[str, Any]) -> list[str] | None:
    """The tracks to separate: an explicit list, or None meaning the scope."""
    raw_ids = payload.get("stable_ids")
    scope = payload.get("scope")
    if raw_ids is not None and scope is not None:
        raise StemsJobPayloadError(
            "give 'stable_ids' or 'scope', not both -- they answer the same "
            "question differently and there is no sane precedence"
        )
    if scope is not None:
        if scope != SCOPE_PENDING:
            raise StemsJobPayloadError(
                f"unknown scope {scope!r}; known: {SCOPE_PENDING!r}"
            )
        return None
    if not isinstance(raw_ids, list) or not raw_ids:
        raise StemsJobPayloadError(
            "stems.separate payload needs a non-empty 'stable_ids' list "
            f"or scope={SCOPE_PENDING!r}; got stable_ids={raw_ids!r}"
        )
    seen: list[str] = []
    for entry in raw_ids:
        if not isinstance(entry, str) or not entry.strip():
            raise StemsJobPayloadError(
                f"stable_ids entries must be non-empty strings; got {entry!r}"
            )
        stable_id = entry.strip()
        # The id becomes a path segment under state/stems/. Checked here at
        # the edge as well as by the artifact reader: a traversal caught at
        # enqueue is a 400, one caught later is a job that already spawned.
        if not is_safe_stable_id_segment(stable_id):
            raise StemsJobPayloadError(
                f"unusable stable_id {stable_id!r}: must be 1-128 URL-safe "
                "identifier characters and usable as one path segment"
            )
        if stable_id not in seen:
            seen.append(stable_id)
    return seen


def _parse_tier(payload: dict[str, Any]) -> str:
    tier = payload.get("tier", DEFAULT_TIER)
    if not isinstance(tier, str) or tier not in MODAL_TIER_KEYS:
        # LOCAL is a real rung of apps/stems/tiers.py and is refused here on
        # purpose: this kind IS the Modal path. A local rung needs a different
        # worker, not a flag on this one, so naming it is a payload error
        # rather than a silent promotion to a card the maintainer pays for.
        raise StemsJobPayloadError(
            f"tier {tier!r} is not a Modal tier; this job kind runs on Modal. "
            f"Known Modal tiers: {', '.join(MODAL_TIER_KEYS)}"
        )
    return tier


def _parse_data_dir(payload: dict[str, Any]) -> Path | None:
    raw_dir = payload.get("data_dir")
    if raw_dir is None:
        return None
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        raise StemsJobPayloadError(f"data_dir must be a path string; got {raw_dir!r}")
    data_dir = Path(raw_dir)
    if not data_dir.is_absolute():
        raise StemsJobPayloadError(f"data_dir must be absolute; got {raw_dir!r}")
    return data_dir


# ----- registration ----------------------------------------------------------


def build_argv(payload: dict[str, Any]) -> list[str]:
    stable_ids, tier, data_dir = parse_payload(payload)
    if shutil.which(UV_BIN) is None:
        raise StemsJobPayloadError(
            f"{UV_BIN!r} is not on PATH, and the stems worker needs it: modal "
            "is deliberately not a repo dependency, so the worker runs under "
            "`uv run --with modal`. Install uv, or set MDT_UV_BIN."
        )
    argv: list[str] = [
        UV_BIN,
        "run",
        "--with",
        "modal",
        "python",
        WORKER_SCRIPT,
        "--tier",
        tier,
    ]
    if data_dir is not None:
        argv += ["--data-dir", str(data_dir)]
    if stable_ids is None:
        return [*argv, "--scope", SCOPE_PENDING]
    for stable_id in stable_ids:
        argv += ["--stable-id", stable_id]
    return argv


def on_progress(_job: dict[str, Any], line: dict[str, Any]) -> None:
    """Turn 'this track's bundle is on disk' into the library's own event.

    The job row is part of the observer contract and unused here: what the
    library needs to know is the track, and the track is in the line.
    """
    stable_id = line.get(PROGRESS_TRACK_KEY)
    if not isinstance(stable_id, str) or not stable_id:
        return
    events.publish(
        "library.changed", {"kind": LIBRARY_KIND, "ids": [stable_id]}
    )


def reconcile_from_disk(job: dict[str, Any]) -> str:
    """What a lost job actually did, read off the filesystem.

    Most kinds cannot answer this and correctly return 'unknown'. This one
    can: a bundle is either on disk or it is not, and that is the entire
    deliverable. So an engine restart mid-run does not strand the row behind a
    human -- which matters here more than elsewhere, because the alternative
    to re-enqueueing is re-paying for the GPU by hand.

    Presence, not full validation: a bundle that is present but corrupt is a
    DIFFERENT failure, and the artifact reader already refuses it at play
    time. Re-running the GPU is not the fix for a bad decode.
    """
    try:
        stable_ids, _tier, data_dir = parse_payload(job.get("payload") or {})
    except StemsJobPayloadError:
        return "unknown"
    if stable_ids is None:
        # A scope job's target set was resolved inside the worker and is gone
        # with it. "Every pending track is now done" cannot be distinguished
        # from "the scope was empty", so the honest answer is the one that
        # makes a human look rather than the one that re-buys GPU time.
        return "unknown"
    root = stems_root(data_dir)
    if all(has_bundle(stable_id, root) for stable_id in stable_ids):
        return "succeeded"
    return "failed"


def stems_root(data_dir: Path | None) -> Path:
    """Where bundles live for a given data dir.

    ``None`` means "whatever MDT_DATA_DIR says", read at CALL time rather than
    import time so a test that repoints the env is not fighting a value this
    module froze on first import.
    """
    if data_dir is not None:
        return data_dir / "state" / "stems"
    from apps.shared.platform_paths import DATA_DIR

    return DATA_DIR / "state" / "stems"


__all__ = [
    "JOB_KIND",
    "LIBRARY_KIND",
    "MODAL_TIER_KEYS",
    "PROGRESS_TRACK_KEY",
    "SCOPE_PENDING",
    "StemsJobPayloadError",
    "build_argv",
    "on_progress",
    "parse_payload",
    "reconcile_from_disk",
    "stems_root",
]
