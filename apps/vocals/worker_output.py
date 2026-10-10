"""The vocal worker's stdout contract: exactly one framed result line.

``scripts/vocal_region_worker.py`` runs torch, demucs and a decoder in its own
process. Any of them may print, and a first run downloads model weights with a
progress bar, so "stdout is pure JSON" was a promise the worker could not keep
for code it does not own. The contract is therefore explicit on both sides:

  * the worker hands every stdout write but its own result to stderr, and
    writes the result as ONE line that starts with :data:`RESULT_FRAME`;
  * the engine accepts stdout only when it holds exactly one such line. It
    never searches for "a line that parses": zero frames, two frames, or a
    frame that is not a JSON object is an error that names the worker, its
    exit code, and what the two streams actually held.

Requirements (mini-PRD):
  ✔︎ ✅ parse exactly one framed result, whatever else is on stdout
    [if] noise precedes one framed line [then] that line's JSON is returned
    [if] stdout is empty and the exit code is 0 [then ⛔️] WorkerOutputError
    [if] stdout holds unframed JSON [then ⛔️] WorkerOutputError, not a guess
    [if] stdout holds two frames [then ⛔️] WorkerOutputError
  ✔︎ ✅ every error is bounded and says what was received
    [if] stderr is megabytes long [then] only its last STDERR_TAIL_CHARS appear

-Claude
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

WORKER_NAME: str = "vocal_region_worker"
# Must equal RESULT_FRAME in scripts/vocal_region_worker.py (a standalone
# PEP 723 script that cannot import this package); a test pins the pair.
RESULT_FRAME: str = "odj-vocal-result-v1 "
STDOUT_EXCERPT_CHARS: int = 200
STDERR_TAIL_CHARS: int = 1200


class WorkerOutputError(RuntimeError):
    """The worker ended without one usable result; the message says why."""


def streams_excerpt(stdout: str | None, stderr: str | None) -> str:
    """A bounded account of both streams for an error message."""
    out = stdout or ""
    err = (stderr or "").strip()
    return f"stdout began {out[:STDOUT_EXCERPT_CHARS]!r} ({len(out)} chars); stderr ended {err[-STDERR_TAIL_CHARS:]!r}"


def parse_worker_stdout(stdout: str, *, returncode: int, stderr: str | None, audio_path: Path) -> dict[str, Any]:
    """Return the one framed result on ``stdout``, or raise saying what was there."""
    framed = [line for line in stdout.splitlines() if line.startswith(RESULT_FRAME)]
    if len(framed) != 1:
        raise WorkerOutputError(
            f"{WORKER_NAME} (exit {returncode}) wrote {len(framed)} result frames for "
            f"{audio_path}, expected exactly 1; {streams_excerpt(stdout, stderr)}"
        )
    body = framed[0][len(RESULT_FRAME) :]

    def not_an_object(why: str) -> WorkerOutputError:
        return WorkerOutputError(
            f"{WORKER_NAME} (exit {returncode}) wrote a result frame that is not a JSON "
            f"object for {audio_path} ({why}); frame began "
            f"{body[:STDOUT_EXCERPT_CHARS]!r}; {streams_excerpt(stdout, stderr)}"
        )

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise not_an_object(str(exc)) from exc
    if not isinstance(result, dict):
        raise not_an_object(f"got {type(result).__name__}")
    return result
