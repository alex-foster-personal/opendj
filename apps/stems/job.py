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
  In a checkout the argv is a ``uv run --with modal`` overlay; in the
  installed app it is the payload's own interpreter and an absolute path
  into the payload (``apps/stems/worker_launch.py``, issue #3421).
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
import sys
from pathlib import Path
from typing import Any

from apps.shared import events
from apps.shared.stable_id import is_safe_stable_id_segment
from apps.stems import worker_launch
from apps.stems.selection import has_bundle
from apps.stems.tiers import DEFAULT_TIER, modal_tiers

# ----- CFG -------------------------------------------------------------------
JOB_KIND: str = "stems.separate"
"""Dotted on purpose: ``<domain>.<verb>``, so a kinds list stays readable once
there is more than one and a UI can group by the part before the dot."""

WORKER_SCRIPT: str = "scripts/stems_modal_worker.py"
LOCAL_WORKER_SCRIPT: str = "scripts/stems_local_worker.py"
HYDRATE_WORKER_SCRIPT: str = "scripts/stems_hydrate_worker.py"
R2_FIRST_WORKER_SCRIPT: str = "scripts/stems_r2_first_worker.py"


def local_worker_script() -> str:
    """The on-device worker as an ABSOLUTE path, anchored on the source tree.

    It used to be spawned as the relative ``scripts/stems_local_worker.py``,
    which is a bet on the cwd of whatever process runs the job. In a checkout
    that bet happened to win. In the installed app the engine runs from ``/``
    and the payload ships no ``scripts/`` directory at all, so the job died
    198 ms after the first-run wizard enqueued it (test Mac, Wed 16 Sep 2026):
    ``can't open file '//scripts/stems_local_worker.py'``. The payload now
    ships it (issue #3421); this still names where it should be either way.
    """
    return str(worker_launch.worker_path(LOCAL_WORKER_SCRIPT))


def local_worker_refusal() -> str | None:
    """Why the on-device worker cannot be spawned from this install, or None.

    Checked where the local stems GATE is checked, so ``GET /stems/plan``
    reports it as ``local_refusal`` and the button renders inert with this
    sentence as its reason -- instead of a job that is accepted, reported as
    started, and fails in a subprocess nobody is watching.
    """
    missing = worker_launch.missing_worker(LOCAL_WORKER_SCRIPT)
    if missing is None:
        return None
    return f"on-device stem separation is not installed in this build: {missing}"

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

TRANSPORT_ENV: str = "MDT_STEMS_TRANSPORT"
DEFAULT_TRANSPORT: str = "relay"
TRANSPORTS: tuple[str, str] = ("relay", "direct")
"""How the worker reaches a GPU. A BUILD-TIME property, not a per-job one.

Read from the engine's environment rather than the job payload on purpose: a
shipped build separates through the relay because it has no Modal credential,
and letting a request name ``direct`` would let any caller ask the app to look
for a credential the tester was never given. the maintainer's own machine sets the env
var once; a tester's build cannot be talked into it by a payload.
"""


class StemsJobPayloadError(ValueError):
    """The enqueue payload cannot describe a run, so no run is started."""


# ----- payload ---------------------------------------------------------------


def _parse_tier(payload: dict[str, Any], *, executor: str) -> str:
    tier = payload.get("tier", DEFAULT_TIER)
    from apps.stems.routing import EXECUTOR_LOCAL, effective_tier

    tier = effective_tier(tier if isinstance(tier, str) else DEFAULT_TIER, executor)
    if executor == EXECUTOR_LOCAL:
        if tier != "LOCAL":
            raise StemsJobPayloadError(
                f"local executor requires tier LOCAL; got {tier!r}"
            )
        return tier
    if not isinstance(payload.get("tier", DEFAULT_TIER), str) or tier not in MODAL_TIER_KEYS:
        # LOCAL is a real rung of apps/stems/tiers.py and is refused here on
        # purpose when the Modal executor is selected: this kind IS the Modal
        # path. A local rung needs a different worker, not a flag on this one.
        raise StemsJobPayloadError(
            f"tier {tier!r} is not a Modal tier; this job kind runs on Modal. "
            f"Known Modal tiers: {', '.join(MODAL_TIER_KEYS)}"
        )
    return tier


def parse_payload(payload: dict[str, Any]) -> tuple[list[str] | None, str, Path | None, str]:
    """Validate payload; fourth value is the resolved executor."""
    from apps.stems.routing import resolve_stems_executor

    executor = resolve_stems_executor()
    return (
        _parse_target(payload),
        _parse_tier(payload, executor=executor),
        _parse_data_dir(payload),
        executor,
    )


def canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize tier and executor for the stored job row and UI."""
    stable_ids, tier, data_dir, executor = parse_payload(payload)
    out = dict(payload)
    out["tier"] = tier
    out["executor"] = executor
    if stable_ids is not None:
        out["stable_ids"] = stable_ids
        out.pop("scope", None)
    elif "scope" in payload:
        out["scope"] = payload["scope"]
        out.pop("stable_ids", None)
    if data_dir is not None:
        out["data_dir"] = str(data_dir)
    return out


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


def resolve_transport() -> str:
    """The build's GPU transport, from the engine environment.

    An unset value means the relay, because that is what a shipped build is.
    An unrecognised value is refused rather than falling back: a typo silently
    resolving to 'relay' would look like it worked right up until someone
    needed 'direct' and could not tell why they were not getting it.
    """
    raw = os.environ.get(TRANSPORT_ENV, "").strip() or DEFAULT_TRANSPORT
    if raw not in TRANSPORTS:
        raise StemsJobPayloadError(
            f"{TRANSPORT_ENV}={raw!r} is not a known transport; "
            f"known: {', '.join(TRANSPORTS)}"
        )
    return raw


def _resolve_data_dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir
    from apps.shared.platform_paths import DATA_DIR

    return DATA_DIR


def _r2_first_argv(
    stable_ids: list[str] | None,
    *,
    tier: str,
    data_dir: Path | None,
    executor: str,
) -> list[str]:
    root = _resolve_data_dir(data_dir)
    # sys.executable: this worker imports nothing heavier than the engine
    # itself does, so the engine's own interpreter runs it, in a checkout and
    # in the installed app alike. Only the PATH has to be anchored.
    missing = worker_launch.missing_worker(R2_FIRST_WORKER_SCRIPT)
    if missing is not None:
        raise StemsJobPayloadError(missing)
    argv: list[str] = [
        sys.executable,
        str(worker_launch.worker_path(R2_FIRST_WORKER_SCRIPT)),
        "--data-dir",
        str(root),
    ]
    if stable_ids is None:
        argv += ["--scope", SCOPE_PENDING]
    else:
        for stable_id in stable_ids:
            argv += ["--stable-id", stable_id]
    argv += ["--tier", tier, "--executor", executor]
    return argv


def compute_argv(
    stable_ids: list[str] | None,
    *,
    tier: str,
    data_dir: Path | None,
    executor: str,
) -> list[str]:
    """The argv that SEPARATES (local Demucs or a Modal GPU), never hydrates.

    Shared with ``scripts/stems_r2_first_worker.py``, which falls back to it
    for tracks R2 does not hold, so the two cannot drift on how a worker is
    found or started.
    """
    from apps.stems.routing import EXECUTOR_LOCAL

    try:
        if executor == EXECUTOR_LOCAL:
            from apps.stems.local_gate import local_stems_gate

            refusal = local_stems_gate() or local_worker_refusal()
            if refusal is not None:
                raise StemsJobPayloadError(refusal)
            argv = worker_launch.script_argv(LOCAL_WORKER_SCRIPT)
        else:
            transport = resolve_transport()
            argv = [
                *worker_launch.script_argv(
                    WORKER_SCRIPT, with_modal=transport == "direct"
                ),
                "--tier",
                tier,
                "--transport",
                transport,
            ]
    except worker_launch.WorkerLaunchError as exc:
        raise StemsJobPayloadError(str(exc)) from exc
    if data_dir is not None:
        argv += ["--data-dir", str(data_dir)]
    if stable_ids is None:
        return [*argv, "--scope", SCOPE_PENDING]
    for stable_id in stable_ids:
        argv += ["--stable-id", stable_id]
    return argv


def build_argv(payload: dict[str, Any]) -> list[str]:
    from apps.cloud.stem_source import resolve_stem_hydration_source

    stable_ids, tier, data_dir, executor = parse_payload(payload)
    root = _resolve_data_dir(data_dir)
    if resolve_stem_hydration_source(root) is not None:
        return _r2_first_argv(stable_ids, tier=tier, data_dir=data_dir, executor=executor)
    return compute_argv(stable_ids, tier=tier, data_dir=data_dir, executor=executor)


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
        stable_ids, _tier, data_dir, _executor = parse_payload(job.get("payload") or {})
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
    "HYDRATE_WORKER_SCRIPT",
    "JOB_KIND",
    "LIBRARY_KIND",
    "MODAL_TIER_KEYS",
    "PROGRESS_TRACK_KEY",
    "R2_FIRST_WORKER_SCRIPT",
    "SCOPE_PENDING",
    "StemsJobPayloadError",
    "build_argv",
    "canonical_payload",
    "compute_argv",
    "local_worker_refusal",
    "local_worker_script",
    "on_progress",
    "parse_payload",
    "reconcile_from_disk",
    "stems_root",
]
