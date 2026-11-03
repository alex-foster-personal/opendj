"""How one chunk of analysis targets becomes an ``apps.analysis.run`` argv.

Split out of ``ingest`` as a pure function: building the command line is
decidable from the chunk alone, so it is worth being able to assert on it
without spawning a subprocess. Running the command stays in ``ingest``,
which owns the job log and the process handling.
"""
from __future__ import annotations

import json
import sys
import tempfile

#: Targets are ``(stable_id, file_path_or_paths)``. An empty stable_id means
#: batch scope: a freshly staged file with no tracks row yet. A tuple of paths
#: carries ordered decode candidates for the unmapped drain.
Chunk = list[tuple[str, str | tuple[str, ...]]]


class CliFailed(RuntimeError):
    """A pipeline CLI exited non-zero, carrying the code it exited with.

    The code is the whole point: ``apps.analysis.run`` distinguishes "I could
    not analyze this file" from "this file was not here to analyze", and the
    caller has to treat those differently.
    """

    def __init__(self, message: str, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode


def _files_argv(
    head: list[str], chunk: Chunk,
) -> tuple[list[str], None]:
    files = [
        path
        for _, target in chunk
        for path in (target if isinstance(target, tuple) else (target,))
    ]
    return [*head, "--files", *files], None


def _candidates_argv(
    head: list[str], chunk: Chunk,
) -> tuple[list[str], str]:
    payload = [
        [sid, list(target) if isinstance(target, tuple) else [target]]
        for sid, target in chunk
    ]
    with tempfile.NamedTemporaryFile(
        "w", suffix=".candidates.json", delete=False
    ) as fh:
        json.dump(payload, fh)
        handoff_path = fh.name
    return [*head, "--candidates-json", handoff_path], handoff_path


def _pairs_argv(
    head: list[str], chunk: Chunk,
) -> tuple[list[str], str]:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".pairs.json", delete=False
    ) as fh:
        json.dump([[sid, target] for sid, target in chunk], fh)
        pairs_path = fh.name
    return [*head, "--pairs-json", pairs_path], pairs_path


def build_analysis_argv(chunk: Chunk, backend: str) -> tuple[list[str], str | None]:
    """Return the argv for ``chunk``, plus a handoff file the caller must delete.

    ``backend`` is passed explicitly rather than left to the CLI's own
    default so the drain's choice is stated at the call site, and so a caller
    can name a backend this machine genuinely cannot run.

    Library scope hands the runner the CANONICAL state-layer stable_ids via
    ``--pairs-json`` or ``--candidates-json``: with ``--files`` it derives
    ``pathid_*`` keys, so the row lands under a key coverage never matches and
    the track re-analyzes on every refresh. Batch scope (empty sids) keeps
    ``--files``, where the ``pathid_*`` placeholder is the intended pre-ingest
    identity.

    The second element is the temp path to unlink, or ``None`` when the
    ``--files`` form was used and there is nothing to clean up.
    """
    head = [sys.executable, "-m", "apps.analysis.run",
            "--backend", backend,
            "--workers", str(min(2, len(chunk)))]
    if not all(sid for sid, _ in chunk):
        return _files_argv(head, chunk)
    multi = any(isinstance(target, tuple) and len(target) > 1 for _, target in chunk)
    if multi or any(isinstance(target, tuple) for _, target in chunk):
        return _candidates_argv(head, chunk)
    return _pairs_argv(head, chunk)
