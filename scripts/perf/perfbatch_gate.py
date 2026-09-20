"""PERFBATCH-02/03 gate: no pipeline throughput/latency change without idle+quality.

A PR that does not touch a stems or lyrics pipeline path is measurement-only
and passes with no markers. A PR that does touch a pipeline-mutation path
must carry both of these on ONE line each, horizontal whitespace only:

    perfbatch: pipelines-idle <non-empty-evidence>
    perfbatch-03: quality-ok reqs=<ID> signal=<path> before_after=<path>

`gh pr view` failing prints UNKNOWN and exits 2, never a silent pass.

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
    return [
        path
        for path in paths
        if _matches_prefix(path, PIPELINE_PREFIXES)
        and not _matches_prefix(path, PIPELINE_EXEMPT)
    ]


def measurement_only_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if _matches_prefix(path, MEASUREMENT_ONLY_PREFIXES)]


def idle_marker(body: str) -> bool:
    return bool(_IDLE.search(body))


def quality_marker(body: str) -> bool:
    return bool(_QUALITY.search(body))


def pr_view(pr: int, repo: str = DEFAULT_REPO) -> dict:
    proc = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "number,title,body"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr view failed rc={proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def pr_diff_names(pr: int, repo: str = DEFAULT_REPO) -> list[str]:
    proc = subprocess.run(
        ["gh", "pr", "diff", str(pr), "--repo", repo, "--name-only"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr diff failed rc={proc.returncode}: {proc.stderr.strip()}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def local_diff_names(repo_root: Path = REPO_ROOT, base: str = DEFAULT_BASE) -> list[str]:
    names: set[str] = set()
    commands = (
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        ["git", "diff", "--name-only"],
        ["git", "diff", "--name-only", "--cached"],
    )
    for argv in commands:
        proc = subprocess.run(
            argv,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            detail = proc.stderr.strip()
            raise RuntimeError(f"{' '.join(argv)} failed rc={proc.returncode}: {detail}")
        names.update(line.strip() for line in proc.stdout.splitlines() if line.strip())
    return sorted(names)


def verdict(paths: list[str], body: str) -> tuple[int, str]:
    mutations = pipeline_mutation_paths(paths)
    if not mutations:
        if paths and len(measurement_only_paths(paths)) == len(paths):
            return 0, "[perfbatch-gate] OK -- measurement-only (declared prefix)"
        return 0, "[perfbatch-gate] OK -- measurement-only"
    missing: list[str] = []
    if not idle_marker(body):
        missing.append("perfbatch: pipelines-idle <evidence>")
    if not quality_marker(body):
        missing.append("perfbatch-03: quality-ok reqs= signal= before_after=")
    if not missing:
        return 0, "[perfbatch-gate] OK -- pipeline mutation cited idle+quality"
    listed = ", ".join(mutations)
    needed = "; ".join(missing)
    return (
        1,
        f"[perfbatch-gate] pipeline-mutation path(s) {listed} need {needed}",
    )


def main(
    argv: list[str] | None = None,
    *,
    fetch=pr_view,
    fetch_files=pr_diff_names,
    diff_names: list[str] | None = None,
    body: str | None = None,
    repo_root: Path = REPO_ROOT,
) -> int:
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
            paths = local_diff_names(repo_root)
        except Exception as exc:
            print(f"[perfbatch-gate] UNKNOWN: could not read local diff ({exc})", file=sys.stderr)
            return 2

    code, message = verdict(paths, pr_body)
    stream = sys.stdout if code == 0 else sys.stderr
    print(message, file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
