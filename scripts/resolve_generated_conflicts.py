"""Resolve merge conflicts in generated files by regenerating them.

Usage::

    python -m scripts.resolve_generated_conflicts
    python -m scripts.resolve_generated_conflicts --repo /path/to/repo

Run inside a merge that stopped on conflicts. Known generated paths are
resolved in a fixed order; unknown conflicted paths are left alone.

REQUIREMENTS
  R-1 Exit 0 only when git reports no conflicted paths remain.         [done]
      [if every known path is regenerated and staged then git --diff-filter=U is empty]
      [if an unknown .py path is still conflicted then exit 1 and name it]
      [if a duplicate requirement id survives union then exit 1 and the file stays conflicted]
  R-2 Generator failure is exit 2, never 0.                            [done]
      [if merge-file fails then exit 2 and REQUIREMENTS.md stays conflicted]
      [if an external command is missing then exit 2]
      [if build_reqs_json raises OSError then exit 2]
  R-3 reqs.json and api-types.ts follow their upstream when it changed.  [done]
      [if REQUIREMENTS.md was resolved then reqs.json is rebuilt even when git merged it clean]
      [if openapi.json was resolved then api-types.ts is rebuilt when its upstream ran]
  R-4 reqs.json written for --repo matches that repo's native build.   [done]
      [if --repo is another checkout then reqs.json "source" is .planning/REQUIREMENTS.md]
  R-5 External generator names are resolved before spawn.                [done]
      [if a bare generator name is not on PATH then exit 2 before spawning]
  R-6 An upstream stays conflicted until its dependent regenerates.     [done]
      [if api-types.ts generation fails then openapi.json stays conflicted and a rerun retries both]
      [if reqs.json regeneration fails then REQUIREMENTS.md stays conflicted]

In-process calls to :mod:`scripts.build_reqs_json` are required: shelling out to
``python -m scripts.build_reqs_json`` with ``cwd`` on a temp repo cannot import
``scripts``, and with ``PYTHONPATH`` set it would rewrite this checkout's reqs.json.
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from scripts import build_reqs_json, debt_index

REQUIREMENTS = ".planning/REQUIREMENTS.md"
REQS_JSON = "reqs.json"
OPENAPI = "apps/webui/openapi.json"
API_TYPES = "apps/webui/frontend/src/lib/api-types.ts"
TECH_DEBT = "(private path)"
ORDER = (REQUIREMENTS, REQS_JSON, OPENAPI, API_TYPES, TECH_DEBT)

UPSTREAM: dict[str, str] = {
    REQS_JSON: REQUIREMENTS,
    API_TYPES: OPENAPI,
}

DEPENDENT: dict[str, str] = {upstream: downstream for downstream, upstream in UPSTREAM.items()}

EXTERNAL_COMMANDS: Mapping[str, tuple[tuple[str, ...], str]] = {
    OPENAPI: (("just", "openapi-dump"), "."),
    API_TYPES: (("pnpm", "run", "api:gen"), "apps/webui/frontend"),
}

_CONFLICT_MARKER = "<<<<<<<"


class GeneratorFailed(Exception):
    """A generator subprocess or I/O step failed."""


class RealConflict(Exception):
    """The merge union is invalid or unmergeable."""


@dataclass
class Outcome:
    resolved: list[str] = field(default_factory=list)
    left: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        if self.failed:
            return 2
        if self.left:
            return 1
        return 0


# ------------------------------------------------------------------ git


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _conflicted_paths(repo: Path) -> list[str]:
    out = _git(repo, "diff", "--name-only", "--diff-filter=U", "-z")
    if out.returncode != 0:
        err = (out.stderr or out.stdout or "git diff failed").strip()
        raise GeneratorFailed(err)
    if not out.stdout:
        return []
    return [p for p in out.stdout.split("\0") if p]


def _stage_text(repo: Path, path: str, stage: int) -> str | None:
    try:
        out = _git(repo, "show", f":{stage}:{path}")
    except UnicodeDecodeError as exc:
        raise GeneratorFailed(f"{path} stage {stage} is not valid UTF-8: {exc}") from exc
    if out.returncode != 0:
        return None
    return out.stdout


def _git_add(repo: Path, path: str) -> None:
    out = _git(repo, "add", "--", path)
    if out.returncode != 0:
        raise GeneratorFailed(f"git add failed for {path}: {out.stderr.strip()}")


# ------------------------------------------------------------------ build_reqs_json seam


@contextlib.contextmanager
def _reqs_paths(root: Path, source: Path, output: Path) -> Iterator[None]:
    saved = (build_reqs_json.REPO_ROOT, build_reqs_json.SOURCE, build_reqs_json.OUTPUT)
    build_reqs_json.REPO_ROOT = root
    build_reqs_json.SOURCE = source
    build_reqs_json.OUTPUT = output
    try:
        yield
    finally:
        build_reqs_json.REPO_ROOT, build_reqs_json.SOURCE, build_reqs_json.OUTPUT = saved


# ------------------------------------------------------------------ step implementations


def _resolve_requirements(repo: Path) -> None:
    ours = _stage_text(repo, REQUIREMENTS, 2)
    theirs = _stage_text(repo, REQUIREMENTS, 3)
    if ours is None or theirs is None:
        raise RealConflict("modify/delete conflict")
    base = _stage_text(repo, REQUIREMENTS, 1)
    if base is None:
        base = ""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ours_f = tmp_path / "ours"
        base_f = tmp_path / "base"
        theirs_f = tmp_path / "theirs"
        ours_f.write_text(ours, encoding="utf-8", newline="")
        base_f.write_text(base, encoding="utf-8", newline="")
        theirs_f.write_text(theirs, encoding="utf-8", newline="")
        mf = subprocess.run(
            ["git", "merge-file", "--union", "-p", str(ours_f), str(base_f), str(theirs_f)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if mf.returncode < 0 or mf.returncode >= 128:
            raise GeneratorFailed(mf.stderr.strip() or f"merge-file exited {mf.returncode}")
        union = mf.stdout

        check_src = tmp_path / "union.md"
        check_src.write_text(union, encoding="utf-8", newline="")
        with _reqs_paths(repo, check_src, tmp_path / "discard.json"):
            try:
                build_reqs_json._build_payload()
            except ValueError as exc:
                raise RealConflict(f"union defines a requirement id twice: {exc}") from exc
            except FileNotFoundError as exc:
                raise GeneratorFailed(str(exc)) from exc
            except OSError as exc:
                raise GeneratorFailed(str(exc)) from exc

    dest = repo / REQUIREMENTS
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(union, encoding="utf-8", newline="")


def _regenerate_reqs_json(repo: Path) -> None:
    source = repo / REQUIREMENTS
    output = repo / REQS_JSON
    with _reqs_paths(repo, source, output):
        try:
            payload = build_reqs_json._build_payload()
            build_reqs_json._write_output(payload)
        except ValueError as exc:
            raise RealConflict(str(exc)) from exc
        except OSError as exc:
            raise GeneratorFailed(str(exc)) from exc


def _run_external(repo: Path, path: str, commands: Mapping[str, tuple[tuple[str, ...], str]]) -> None:
    if path not in commands:
        raise GeneratorFailed(f"no external command registered for {path}")
    argv, rel_cwd = commands[path]
    exe = shutil.which(argv[0])
    if exe is None:
        raise GeneratorFailed(f"command not found: {argv[0]!r} is not on PATH or not executable")
    cwd = repo / rel_cwd
    try:
        proc = subprocess.run(
            [exe, *argv[1:]],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise GeneratorFailed(str(exc)) from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        tail = err[-500:] if len(err) > 500 else err
        raise GeneratorFailed(f"command {argv!r} exited {proc.returncode}: {tail}")
    target = repo / path
    if not target.is_file():
        raise GeneratorFailed(f"generator did not write {path}")
    content = target.read_text(encoding="utf-8")
    if _CONFLICT_MARKER in content:
        raise GeneratorFailed("generator left conflict markers")


def _regenerate_tech_debt(repo: Path) -> None:
    debt_dir = repo / ".planning" / "debt"
    debt_dir.mkdir(parents=True, exist_ok=True)
    try:
        text = debt_index.render_index(debt_dir)
    except (OSError, ValueError) as exc:
        raise GeneratorFailed(str(exc)) from exc
    dest = repo / TECH_DEBT
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8", newline="")


_STEP_RUNNERS: dict[str, Callable[..., None]] = {
    REQUIREMENTS: _resolve_requirements,
    REQS_JSON: _regenerate_reqs_json,
    TECH_DEBT: _regenerate_tech_debt,
}


def _should_run(path: str, conflicted: set[str], done: set[str]) -> bool:
    if path in conflicted:
        return True
    if path == REQS_JSON and REQUIREMENTS in done:
        return True
    return bool(path == API_TYPES and OPENAPI in done)


def _upstream_blocked(path: str, blocked: set[str]) -> str | None:
    upstream = UPSTREAM.get(path)
    if upstream and upstream in blocked:
        return upstream
    return None


def _dependent_pending(dependent: str, outcome: Outcome) -> bool:
    return dependent in outcome.failed or dependent in outcome.left


def _stage_done(repo: Path, done: set[str], outcome: Outcome) -> None:
    # Dependents stage before upstreams: upstream stays conflicted until dependent is staged.
    staged: set[str] = set()
    for path in reversed(ORDER):
        if path not in done:
            continue
        dependent = DEPENDENT.get(path)
        if dependent is not None and dependent not in staged and _dependent_pending(dependent, outcome):
            outcome.left[path] = f"held conflicted until dependent {dependent} regenerates"
            continue
        try:
            _git_add(repo, path)
        except GeneratorFailed as exc:
            outcome.failed[path] = str(exc)
            continue
        staged.add(path)
    outcome.resolved.extend(p for p in ORDER if p in staged)


def _run_step(
    repo: Path,
    path: str,
    commands: Mapping[str, tuple[tuple[str, ...], str]],
) -> None:
    if path in (OPENAPI, API_TYPES):
        _run_external(repo, path, commands)
        return
    runner = _STEP_RUNNERS[path]
    runner(repo)  # type: ignore[call-arg]


def resolve(
    repo: Path,
    *,
    commands: Mapping[str, tuple[tuple[str, ...], str]] = EXTERNAL_COMMANDS,
) -> Outcome:
    try:
        conflicted_set = set(_conflicted_paths(repo))
    except GeneratorFailed as exc:
        return Outcome(failed={"<repo>": str(exc)})
    outcome = Outcome()
    if not conflicted_set:
        return outcome

    done: set[str] = set()
    blocked: set[str] = set()

    for path in ORDER:
        if not _should_run(path, conflicted_set, done):
            continue
        blocked_upstream = _upstream_blocked(path, blocked)
        if blocked_upstream is not None:
            if path in conflicted_set:
                outcome.left[path] = f"upstream {blocked_upstream} unresolved"
                blocked.add(path)
            continue
        try:
            _run_step(repo, path, commands)
        except RealConflict as exc:
            outcome.left[path] = str(exc)
            blocked.add(path)
            continue
        except (GeneratorFailed, OSError, subprocess.SubprocessError, UnicodeError) as exc:
            outcome.failed[path] = str(exc)
            blocked.add(path)
            continue
        done.add(path)

    _stage_done(repo, done, outcome)

    known = set(ORDER)
    for path in sorted(conflicted_set - known):
        outcome.left[path] = "not a generated path this tool knows"

    return outcome


def _format_report(outcome: Outcome, remaining: list[str]) -> str:
    lines: list[str] = []
    for path in ORDER:
        if path in outcome.resolved:
            lines.append(f"[OK] resolved {path}")
        elif path in outcome.failed:
            lines.append(f"[UNKNOWN] {path}: generator failed: {outcome.failed[path]}")
        elif path in outcome.left:
            lines.append(f"[LEFT] {path}: {outcome.left[path]}")
    known = set(ORDER)
    lines.extend(
        f"[LEFT] {path}: {outcome.left.get(path, 'still conflicted')}"
        for path in sorted(remaining)
        if path not in known
    )
    if not remaining:
        lines.append("[OK] no conflicted paths remain")
    else:
        names = ", ".join(remaining)
        lines.append(f"[LEFT] {len(remaining)} conflicted path(s) remain: {names}")
    return "\n".join(lines)


def _repo_root_from_cwd() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise GeneratorFailed("not inside a git repository")
    return Path(out.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve generated-file merge conflicts.")
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Repository root (default: git rev-parse --show-toplevel)",
    )
    args = parser.parse_args(argv)
    try:
        repo = args.repo if args.repo is not None else _repo_root_from_cwd()
    except GeneratorFailed as exc:
        print(f"[UNKNOWN] {exc}", file=sys.stderr)
        return 2

    repo = repo.resolve()
    outcome = resolve(repo, commands=EXTERNAL_COMMANDS)
    if outcome.failed.get("<repo>"):
        print(f"[UNKNOWN] repository: {outcome.failed['<repo>']}")
        return 2
    try:
        remaining = _conflicted_paths(repo)
    except GeneratorFailed as exc:
        print(f"[UNKNOWN] repository: {exc}")
        return 2
    print(_format_report(outcome, remaining))

    if outcome.failed:
        return 2
    if remaining:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
