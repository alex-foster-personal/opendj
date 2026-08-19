#!/usr/bin/env python3
"""Engine job worker: separate N tracks on Modal, one bundle at a time.

This is the process behind the ``stems.separate`` job kind
(``apps/stems/job.py``). The engine spawns it, reads its stdout, and turns
each line into job progress and a ``library.changed`` event. It exists as a
SUBPROCESS rather than engine code for one reason: ``modal`` is not a repo
dependency and must never become one, so every call site is a
``uv run --with modal`` overlay.

Reuse, not reinvention. The Modal app, the baked-weights image, the GPU
function and the bundle writer all come from ``scripts/modal_vocal_farm.py``.
Importing them means a run here is a cache HIT on the same image layers the
farm built, and a bundle written here is byte-for-byte the same contract as a
bundle written by the farm. A second Modal app defined next door would cost a
fresh multi-minute image build and a second manifest writer to keep in sync.

The farm's own CLI is not used because it selects tracks by the VOCAL-cache
gap: ``--only-stable-id`` refuses an id that is not in that gap, which is the
wrong question for a stems job. Here the caller names the tracks.

STDOUT IS A PROTOCOL, NOT A LOG. The runner kills the job on any line that is
not ``{"progress": <number>, ...}``, and Modal's client prints its own
progress to fd 1. So fd 1 is redirected to stderr at startup and the protocol
is written through a private duplicate. Nothing any library prints can
corrupt the stream.

Requirements (mini-PRD):
  ✔︎ ✅ every named track is resolved to a real audio file before any GPU
    time is bought.
    [if] a stable id is not in state.db [then ⛔️] exit non-zero, spend nothing
    [if] its file_path is missing from disk [then ⛔️] same
  ✔︎ ✅ one progress line per completed track, naming that track.
    [if] track 3 of 10 lands [then] progress 0.3 and stems_ready names it
    [if] a track fails in-container [then] the run continues and exits 1 at
      the end with every failure named
  ✔︎ ✅ a bundle is written where the webui reads it, in the farm's own
    schema-2 layout, and is never fabricated.
    [if] Modal returns no stems for a track [then ⛔️] that track fails
  ✔︎ ✅ credentials are checked before the batch, not per track.
    [if] no Modal token is configured [then ⛔️] exit 2 with the fix

Run (modal is not a repo dependency -- overlay it):
  uv run --with modal python scripts/stems_modal_worker.py \
      --tier M --stable-id <id> [--stable-id <id> ...] [--data-dir DIR]

-Claude
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # A worker is spawned with the engine's cwd, which is not guaranteed to be
    # the repo root. This is the script-entrypoint case the import rules carve
    # out, not a mid-import sys.path fix in a library.
    sys.path.insert(0, str(REPO_ROOT))

# ----- CFG -------------------------------------------------------------------
SEPARATOR_ENV: str = "MDT_STEMS_SEPARATOR"
"""Test seam, and the ONLY one. ``module:attr`` naming a replacement for the
Modal call. Unset in every real run, which is what makes the default path the
real GPU rather than something that can silently degrade to a stand-in.

It exists because the alternative is worse: without it, a test of the job
pipeline would have to fake the pipeline instead of the vendor, and the thing
most likely to break (enqueue -> spawn -> progress -> bundle -> event) would
be the part not covered. Only the vendor boundary moves.
"""

EXIT_BAD_INPUT: int = 1
EXIT_NO_CREDENTIALS: int = 2

# Progress is reported against tracks completed, and stops just short of 1.0:
# the runner writes the terminal 1.0 itself when the process exits clean, so a
# worker claiming 1.0 early would show 'done' with work still running.
PROGRESS_CEILING: float = 0.99


@dataclass(frozen=True)
class TrackJob:
    """One track this run will separate."""

    stable_id: str
    audio_path: Path
    duration_ms: int
    size_bytes: int


# ----- stdout protocol -------------------------------------------------------


class _Protocol:
    """The runner's progress channel, isolated from every other writer.

    fd 1 is pointed at stderr and the real stdout is kept as a private
    duplicate. Modal's output manager, a stray print in a dependency, a
    warning -- all land in the error tail where they are useful, and none of
    them can turn into a protocol violation that kills a job mid-GPU.
    """

    def __init__(self) -> None:
        self._out = os.fdopen(os.dup(1), "w", encoding="utf-8")
        os.dup2(2, 1)
        sys.stdout = sys.stderr

    def emit(self, progress: float, message: str, **extra: Any) -> None:
        line = json.dumps(
            {"progress": round(float(progress), 4), "message": message, **extra},
            separators=(",", ":"),
        )
        self._out.write(line + "\n")
        self._out.flush()

    def close(self) -> None:
        self._out.close()


# ----- track resolution ------------------------------------------------------


def select_tracks(
    stable_ids: Sequence[str] | None, scope: str | None, data_dir: Path
) -> list[TrackJob]:
    """The batch this run will separate, from ids or from the pending scope."""
    if (stable_ids is None) == (scope is None):
        raise SystemExit(
            "error: pass exactly one of --stable-id (repeatable) or --scope"
        )
    if stable_ids is not None:
        return resolve_tracks(stable_ids, data_dir)

    from apps.stems.selection import library_buckets

    buckets = library_buckets(data_dir)
    # The denominator, on stderr where a reader looking at a short run finds
    # it. A scope run that separates 12 of 8000 rows is only alarming until
    # you can see that 7100 were already done and 888 have no audio.
    print(
        f"[stems-worker] scope={scope}: {len(buckets.pending)} pending, "
        f"{len(buckets.ready)} already done, "
        f"{len(buckets.unavailable)} with no audio on disk, "
        f"of {buckets.total} library rows",
        file=sys.stderr,
        flush=True,
    )
    return [
        TrackJob(
            stable_id=candidate.stable_id,
            audio_path=candidate.audio_path,
            duration_ms=candidate.duration_ms,
            size_bytes=candidate.audio_path.stat().st_size,
        )
        for candidate in buckets.pending
    ]


def resolve_tracks(stable_ids: Sequence[str], data_dir: Path) -> list[TrackJob]:
    """stable_id -> a real file on disk, or a hard refusal naming what broke.

    Read-only against state.db and strict about both halves: an id the library
    does not know and an id whose file has moved are different failures and
    are reported as different failures. Neither is skipped -- a job asked for
    ten tracks and quietly separating eight is how a caller comes to believe
    it has stems it does not have.
    """
    state_db = data_dir / "state" / "state.db"
    if not state_db.is_file():
        raise SystemExit(f"error: state.db missing: {state_db}")
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" for _ in stable_ids)
        rows = connection.execute(
            "SELECT stable_id, file_path, duration_ms FROM tracks "
            f"WHERE stable_id IN ({placeholders})",
            tuple(stable_ids),
        ).fetchall()
    finally:
        connection.close()

    found = {row[0]: row for row in rows}
    unknown = [sid for sid in stable_ids if sid not in found]
    if unknown:
        raise SystemExit(
            f"error: {len(unknown)} stable_id(s) are not in {state_db}: "
            f"{', '.join(unknown[:10])}"
        )

    jobs: list[TrackJob] = []
    missing: list[str] = []
    for stable_id in stable_ids:
        _sid, file_path, duration_ms = found[stable_id]
        if not file_path:
            missing.append(f"{stable_id} (no file_path)")
            continue
        path = Path(file_path)
        if not path.is_file():
            missing.append(f"{stable_id} -> {file_path}")
            continue
        jobs.append(
            TrackJob(
                stable_id=stable_id,
                audio_path=path,
                duration_ms=int(duration_ms or 0),
                size_bytes=path.stat().st_size,
            )
        )
    if missing:
        raise SystemExit(
            "error: no audio on disk for "
            f"{len(missing)} track(s): {'; '.join(missing[:10])}"
        )
    return jobs


# ----- the Modal boundary ----------------------------------------------------

# (tracks, tier_key) -> yields one result dict per track, in input order.
# The dict is exactly what farm.separate_track returns with a local
# destination: {stable_id, stems: {part: bytes}, audio: {...},
# source_sha256, ...} or {stable_id, error}.
Separator = Callable[[Sequence[TrackJob], str], Iterator[dict[str, Any]]]


def load_separator() -> tuple[Separator, str]:
    """The real Modal separator, unless a test has named a replacement."""
    override = os.environ.get(SEPARATOR_ENV, "").strip()
    if not override:
        return modal_separator, "modal"
    if ":" not in override:
        raise SystemExit(
            f"error: {SEPARATOR_ENV}={override!r} must be 'module:attr'"
        )
    module_name, _, attr = override.partition(":")
    try:
        module = importlib.import_module(module_name)
        separator = getattr(module, attr)
    except (ImportError, AttributeError) as exc:
        raise SystemExit(
            f"error: {SEPARATOR_ENV}={override!r} is not importable: {exc}"
        ) from exc
    return separator, override


def assert_modal_credentials() -> None:
    """Refuse before the batch if this machine cannot authenticate.

    Checked once, here, rather than being discovered by the first container:
    an auth failure is the same failure for every track, so reporting it as
    'track 1 failed' would send a reader looking at the track.
    """
    from modal.config import config

    if config.get("token_id") and config.get("token_secret"):
        return
    print(
        "error: no Modal credentials on this machine, so no stems can be "
        "separated.\n"
        "  Neither ~/.modal.toml nor MODAL_TOKEN_ID/MODAL_TOKEN_SECRET is set.\n"
        "  Fix (either one):\n"
        "    uv tool install modal && modal token new"
        "        # browser auth, writes ~/.modal.toml\n"
        "    export MODAL_TOKEN_ID=... MODAL_TOKEN_SECRET=..."
        "  # or put both in Doppler general/dev_personal",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(EXIT_NO_CREDENTIALS)


def modal_separator(
    tracks: Sequence[TrackJob], tier_key: str
) -> Iterator[dict[str, Any]]:
    """Fan these tracks out over the farm's GPU function, longest first.

    ``.starmap`` with ``order_outputs=True`` is what makes a live progress bar
    possible: results arrive as containers finish, but attributable, because
    ordered output means result i belongs to input i. Unordered output would
    be marginally faster and would make a Modal-level failure anonymous, which
    costs more than it saves when the whole point is per-track UI.

    ``stems_dest='local'`` tells the container to RETURN the stem bytes rather
    than upload them; this process writes them where the webui reads them.
    """
    import scripts.modal_vocal_farm as farm

    assert_modal_credentials()
    preset = preset_for_tier(tier_key)
    ordered = farm.sort_longest_first(
        [
            farm.GapTrack(
                stable_id=track.stable_id,
                audio_path=track.audio_path,
                duration_ms=track.duration_ms,
                size_bytes=track.size_bytes,
            )
            for track in tracks
        ]
    )

    def _inputs() -> Iterator[tuple[Any, ...]]:
        for track in ordered:
            yield (
                track.audio_path.read_bytes(),
                track.stable_id,
                track.audio_path.name,
                preset.model,
                preset.overlap,
                preset.shifts,
                "local",
                str(track.audio_path),
                preset.stamp(),
                "",  # r2_bucket: unused for a local destination
                farm.DEFAULT_STEM_CODEC,
            )

    with farm.app.run():
        yield from farm.separate_track.starmap(_inputs(), order_outputs=True)


def preset_for_tier(tier_key: str) -> Any:
    """The farm Preset behind a tiers.py rung, refusing an unbaked model."""
    import scripts.modal_vocal_farm as farm
    from apps.stems.tiers import get_tier

    return farm._resolve_preset(get_tier(tier_key).preset_tag, False)


# ----- run -------------------------------------------------------------------


def run(
    tracks: list[TrackJob],
    tier_key: str,
    data_dir: Path,
    protocol: _Protocol,
) -> int:
    """Separate every track, writing and announcing each as it lands."""
    import scripts.modal_vocal_farm as farm

    separator, separator_name = load_separator()
    preset = preset_for_tier(tier_key)
    if separator_name != "modal":
        # Loud in the error tail, AND stamped into every manifest this run
        # writes. The log line warns whoever is watching; the stamp is what
        # protects everyone who is not. A bundle carries its own provenance
        # forever, so a bundle that did not come from the model it names is a
        # lie that outlives the run that told it -- overwriting the model name
        # is what makes an override impossible to mistake for a real
        # separation, at play time and in the Stems column alike.
        print(
            f"[stems-worker] SEPARATOR OVERRIDE ACTIVE: {separator_name}. "
            "This run does NOT touch Modal, and every manifest it writes is "
            "stamped as a test double.",
            file=sys.stderr,
            flush=True,
        )
        preset = dataclasses.replace(
            preset,
            model=f"test-double:{separator_name}",
            tag=f"test-double:{preset.tag}",
        )
    total = len(tracks)
    protocol.emit(
        0.0,
        f"0/{total} stems separated (tier {tier_key}, {preset.tag})",
    )

    done = 0
    failures: list[str] = []
    started = time.perf_counter()
    for result in separator(tracks, tier_key):
        stable_id = result.get("stable_id", "<unknown>")
        error = result.get("error")
        if error:
            failures.append(f"{stable_id}: {error}")
            protocol.emit(
                min(PROGRESS_CEILING, (done + len(failures)) / total),
                f"{done}/{total} stems separated, {len(failures)} failed",
            )
            continue
        track = next(t for t in tracks if t.stable_id == stable_id)
        written, _elapsed = farm._publish_stems_local(
            stable_id, track.audio_path, result, preset, data_dir
        )
        done += 1
        protocol.emit(
            min(PROGRESS_CEILING, (done + len(failures)) / total),
            f"{done}/{total} stems separated",
            # The engine turns THIS key into library.changed for one track, so
            # the row lights up now rather than when the batch ends.
            stems_ready=stable_id,
            bytes_written=written,
        )

    wall = time.perf_counter() - started
    print(
        f"[stems-worker] {done}/{total} separated in {wall:.1f}s, "
        f"{len(failures)} failed",
        file=sys.stderr,
        flush=True,
    )
    if failures:
        print(
            "[stems-worker] failures:\n  " + "\n  ".join(failures),
            file=sys.stderr,
            flush=True,
        )
        return EXIT_BAD_INPUT
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/stems_modal_worker.py",
        description="Separate named tracks on Modal for the stems.separate job",
    )
    parser.add_argument(
        "--stable-id",
        action="append",
        dest="stable_ids",
        default=None,
        help="repeatable; the tracks to separate",
    )
    parser.add_argument(
        "--scope",
        default=None,
        choices=("pending",),
        help="separate every track with audio on disk and no bundle yet, "
        "resolved NOW rather than at enqueue time",
    )
    parser.add_argument(
        "--tier", default="M", help="rung from apps/stems/tiers.py (default M)"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="override the data dir (default: MDT_DATA_DIR)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    protocol = _Protocol()
    try:
        if args.data_dir is not None:
            data_dir = args.data_dir
        else:
            from apps.shared.platform_paths import DATA_DIR

            data_dir = DATA_DIR
        tracks = select_tracks(args.stable_ids, args.scope, data_dir)
        if not tracks:
            # Not a failure. "Nothing to separate" is the correct outcome of a
            # scope job on a library that is already done, and the install
            # flow enqueues one of those every time it re-runs.
            protocol.emit(PROGRESS_CEILING, "0 tracks need stems")
            return 0
        return run(tracks, args.tier, data_dir, protocol)
    finally:
        protocol.close()


if __name__ == "__main__":
    raise SystemExit(main())
