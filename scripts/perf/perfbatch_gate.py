"""PERFBATCH-02/03 gate: no pipeline throughput/latency change without idle+quality.

A PR that does not touch a stems or lyrics pipeline path is measurement-only
and passes with no markers. A PR that does touch a pipeline path is classified
by the CONTENT of its hunks (PERFBATCH-07): when every added or removed line in
that file is on the narrow allowlist below, the touch is non-behavioral and
needs no markers. Anything else, including a hunk the gate cannot read, is a
pipeline mutation and must carry both of these on ONE line each, horizontal
whitespace only:

    perfbatch: pipelines-idle <non-empty-evidence>
    perfbatch-03: quality-ok reqs=<ID> signal=<path> before_after=<path>

Allowlist (see docs/perf/perfbatch-gates.md for the reasoning and residuals):

    Python   blank lines; ``#`` comments that are not a shebang and not a
             PEP 723 metadata line; docstring lines whose opener follows a
             ``def``/``class`` header (or opens the module); widening of an
             ``except (...)`` / ``contextlib.suppress(...)`` tuple to a
             strict superset that adds no broad base; wrapping an existing
             import in ``try: ... except ImportError: <name> = None``.
    Shell    blank lines; ``#`` comments that are not a shebang.
    TS/JS    blank lines; ``//`` line comments (never ``*`` block lines).
    Markdown every line (prose is never executed).
    Other    nothing: any changed line is a mutation.

The gate fails closed: a pipeline path with no parseable hunks (binary,
rename-only, mode-only, or a name list with no diff text) is a mutation.
``gh pr view`` / ``gh pr diff`` / ``git diff`` failing prints UNKNOWN and
exits 2, never a silent pass.

    python -m scripts.perf.perfbatch_gate --pr N
    python -m scripts.perf.perfbatch_gate --diff
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from scripts.perf.perfbatch_hunks import FileDiff, classify_pipeline_paths, parse_unified_diff

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPO = "maintainer/music-dj-tools"
DEFAULT_BASE = "origin/main"

PIPELINE_PREFIXES: tuple[str, ...] = (
    "apps/stems/",
    "apps/lyrics/",
    "scripts/stems_modal_worker.py",
    "scripts/stems_local_worker.py",
    "scripts/stem_bundle_worker.py",
    "scripts/stem_farm_runner.py",
    "scripts/stem_farm_watch_pull_down.sh",
    "scripts/modal_vocal_farm.py",
    "scripts/modal_demucs_ab.py",
    "apps/webui/server/lyric_index_autostart.py",
    "apps/webui/frontend/src/lib/components/rb/wave/lyrics-fetch.svelte.ts",
)

# Install-time machine capability (LATENCY-04) is not a stems/lyrics throughput
# or latency pipeline mutation: it benchmarks once, persists a JSON record, and
# exposes a read-only route. PERFBATCH-02/03 still apply to real pipeline edits.
PIPELINE_EXEMPT: tuple[str, ...] = (
    "apps/stems/live_capability.py",
    "apps/stems/live_capability_api.py",
    # Backward-compat kwarg restore only (``root=``); no throughput/latency change.
    "apps/lyrics/register_stems.py",
)

# AGT-01 (issue #2123): luna primer plus persona checker. Markdown lives under
# ops/agentic-testing/; the importable checker lives under ops/agentic_testing/.
# Neither path changes stems or lyrics throughput or latency.
#
# PERF-UI-01 (issue #2303): short-viewport /performance layout (compact
# waverow, library panel auto-collapse, history list hide). Viewport CSS/DOM
# only; no stems or lyrics throughput or latency change.
MEASUREMENT_ONLY_PREFIXES: tuple[str, ...] = (
    "scripts/perf/",
    "tests/perf/",
    "tests/fixtures/perf/",
    "docs/perf/",
    "specs/perf-latency-program.md",
    ".planning/REQUIREMENTS.md",
    "reqs.json",
    "justfile",
    ".github/workflows/perfbatch-gate.yml",
    "ops/agentic-testing/",
    "ops/agentic_testing/",
    "tests/agentic_testing/",
    "docs/decisions/",
    "specs/",
)

# Horizontal whitespace only. Never `\\s`: a newline as the "reason" must fail.
_IDLE = re.compile(r"perfbatch:[ \t]*pipelines-idle[ \t]+\S", re.IGNORECASE)
_QUALITY = re.compile(
    r"perfbatch-03:[ \t]*quality-ok[ \t]+reqs=\S+[ \t]+signal=\S+[ \t]+before_after=\S+",
    re.IGNORECASE,
)


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized == prefix or normalized.startswith(prefix) for prefix in prefixes)


def pipeline_mutation_paths(paths: list[str]) -> list[str]:
    """Pipeline-prefixed paths that are not file-level exempt (the hunk rule applies next)."""
    return [
        path
        for path in paths
        if _matches_prefix(path, PIPELINE_PREFIXES) and not _matches_prefix(path, PIPELINE_EXEMPT)
    ]


def measurement_only_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if _matches_prefix(path, MEASUREMENT_ONLY_PREFIXES)]


def idle_marker(body: str) -> bool:
    return bool(_IDLE.search(body))


def quality_marker(body: str) -> bool:
    return bool(_QUALITY.search(body))


# ----------------------------------------------------------------------------
# Data sources


def _run(argv: list[str], *, cwd: Path | None = None) -> str:
    # git and gh emit UTF-8 regardless of the console code page; decoding with
    # the Windows default (cp1252) raised inside the reader thread and left
    # stdout empty, which the gate then read as "no diff text" (fail-closed,
    # but for the wrong reason). Undecodable bytes become U+FFFD, which is
    # never on the allowlist, so a garbled code line still fails closed.
    proc = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed rc={proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def pr_view(pr: int, repo: str = DEFAULT_REPO) -> dict:
    out = _run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "number,title,body"])
    return json.loads(out)


def pr_diff_names(pr: int, repo: str = DEFAULT_REPO) -> list[str]:
    out = _run(["gh", "pr", "diff", str(pr), "--repo", repo, "--name-only"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def pr_diff_text(pr: int, repo: str = DEFAULT_REPO) -> str:
    return _run(["gh", "pr", "diff", str(pr), "--repo", repo])


def _merge_base(repo_root: Path, base: str) -> str:
    return _run(["git", "merge-base", base, "HEAD"], cwd=repo_root).strip()


def local_diff_names(repo_root: Path = REPO_ROOT, base: str = DEFAULT_BASE) -> list[str]:
    """Net change of the working tree (committed, staged, unstaged) against the merge base."""
    out = _run(["git", "diff", "--name-only", _merge_base(repo_root, base)], cwd=repo_root)
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def local_diff_text(paths: list[str], repo_root: Path = REPO_ROOT, base: str = DEFAULT_BASE) -> str:
    return _run(["git", "diff", _merge_base(repo_root, base), "--", *paths], cwd=repo_root)


# ----------------------------------------------------------------------------
# Verdict


def verdict(
    paths: list[str], body: str, file_diffs: dict[str, FileDiff] | None = None
) -> tuple[int, str]:
    candidates = pipeline_mutation_paths(paths)
    if not candidates:
        if paths and len(measurement_only_paths(paths)) == len(paths):
            return 0, "[perfbatch-gate] OK -- measurement-only (declared prefix)"
        return 0, "[perfbatch-gate] OK -- measurement-only"
    verdicts = classify_pipeline_paths(candidates, file_diffs)
    mutations = [v for v in verdicts if not v.benign]
    if not mutations:
        listed = ", ".join(v.describe() for v in verdicts)
        return 0, f"[perfbatch-gate] OK -- measurement-only; allowlisted hunks only in {listed}"
    missing: list[str] = []
    if not idle_marker(body):
        missing.append("perfbatch: pipelines-idle <evidence>")
    if not quality_marker(body):
        missing.append("perfbatch-03: quality-ok reqs= signal= before_after=")
    if not missing:
        return 0, "[perfbatch-gate] OK -- pipeline mutation cited idle+quality"
    listed = ", ".join(v.describe() for v in mutations)
    needed = "; ".join(missing)
    return (
        1,
        f"[perfbatch-gate] pipeline-mutation path(s) {listed} need {needed}",
    )


def main(  # noqa: PLR0913 -- every keyword is a test seam for one data source
    argv: list[str] | None = None,
    *,
    fetch=pr_view,
    fetch_files=pr_diff_names,
    fetch_diff=pr_diff_text,
    diff_names: list[str] | None = None,
    diff_text: str | None = None,
    body: str | None = None,
    repo_root: Path = REPO_ROOT,
    base: str = DEFAULT_BASE,
) -> int:
    """``diff_names`` injected without ``diff_text`` fails closed: no hunks, so mutation."""
    parser = argparse.ArgumentParser(prog="perfbatch_gate")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args(argv)
    if not args.pr and not args.diff:
        print("[perfbatch-gate] UNKNOWN: pass --pr N or --diff", file=sys.stderr)
        return 2

    pr_body = body if body is not None else ""
    paths = list(diff_names) if diff_names is not None else None
    text = diff_text if diff_text is not None else ("" if diff_names is not None else None)

    if args.pr:
        try:
            pr = fetch(args.pr, args.repo)
        except Exception as exc:
            print(
                f"[perfbatch-gate] UNKNOWN: could not read PR #{args.pr} ({exc})",
                file=sys.stderr,
            )
            return 2
        pr_body = body if body is not None else (pr.get("body") or "")
        if paths is None:
            try:
                paths = fetch_files(args.pr, args.repo)
            except Exception as exc:
                print(
                    f"[perfbatch-gate] UNKNOWN: could not list PR #{args.pr} files ({exc})",
                    file=sys.stderr,
                )
                return 2

    if paths is None:
        try:
            paths = local_diff_names(repo_root, base)
        except Exception as exc:
            print(f"[perfbatch-gate] UNKNOWN: could not read local diff ({exc})", file=sys.stderr)
            return 2

    candidates = pipeline_mutation_paths(paths)
    if candidates and text is None:
        try:
            if args.pr:
                text = fetch_diff(args.pr, args.repo)
            else:
                text = local_diff_text(candidates, repo_root, base)
        except Exception as exc:
            listed = ", ".join(candidates)
            print(
                f"[perfbatch-gate] UNKNOWN: could not read hunks for {listed} ({exc})",
                file=sys.stderr,
            )
            return 2

    file_diffs = parse_unified_diff(text) if text is not None else None
    code, message = verdict(paths, pr_body, file_diffs)
    stream = sys.stdout if code == 0 else sys.stderr
    print(message, file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
