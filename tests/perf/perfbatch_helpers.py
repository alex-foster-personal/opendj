"""Shared fixtures for the PERFBATCH gate tests (path-level and hunk-level)."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

from scripts.perf import perfbatch_gate as mod

IDLE = "perfbatch: pipelines-idle jobs.db empty on builder 2026-09-11T07:06:49+00:00"
QUALITY = (
    "perfbatch-03: quality-ok reqs=PERFBATCH-01 "
    "signal=scripts/bench/judgement-calibration.json "
    "before_after=docs/research/local-stems-20260908-nucbox-ledger.md"
)
LOCAL_WORKER = "scripts/stems_local_worker.py"
STEMS_JOB = "apps/stems/job.py"
LYRICS_FETCH = "apps/webui/frontend/src/lib/components/rb/wave/lyrics-fetch.svelte.ts"
FARM_WATCH = "scripts/stem_farm_watch_pull_down.sh"


def _diff(
    path: str,
    lines: list[str],
    *,
    old_start: int = 10,
    new_start: int = 10,
    old_path: str | None = None,
) -> str:
    """One-file unified diff; each entry in ``lines`` carries its own tag character."""
    old_path = old_path or path
    return (
        f"diff --git a/{old_path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{old_path}\n"
        f"+++ b/{path}\n"
        f"@@ -{old_start},3 +{new_start},3 @@ def run(\n" + "\n".join(lines) + "\n"
    )


# The only touch PR #3331 (branch af--windows-b0) made to the local worker.
OS_NICE_HUNK = _diff(
    LOCAL_WORKER,
    [
        "     track_timeout_s: float = 3600.0,",
        " ) -> int:",
        '     """Separate every track locally, skipping fresh bundles."""',
        "-    with contextlib.suppress(OSError):",
        "+    # os.nice does not exist on Windows at all (AttributeError, not OSError) -",
        "+    # guard both: absent priority-lowering is a no-op, never a crash.",
        "+    with contextlib.suppress(OSError, AttributeError):",
        "         os.nice(NICE_LEVEL)",
        " ",
        "     root = _stems_root(data_dir)",
    ],
    old_start=111,
    new_start=111,
)


def _gate(paths: list[str], text: str | None, body: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mod.main(["--diff"], diff_names=paths, diff_text=text, body=body)
    return rc, out.getvalue(), err.getvalue()
