"""Mechanical CI fixer for mis-numbered requirement ids and missing bundle charges.

    python -m scripts.ci_mechanical_fix --pr N [--base main] [--frontend-log PATH] [--dry-run]

Exit 0 fixed, 2 checks red (tree restored), 3 nothing mechanical or human decision, 4 unknown.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from scripts.req_id_pr_number import CARRIED, SOURCE, _show, carried_ids, check, defined_ids

_FAIL_RENAME = re.compile(r"^\[FAIL\] new requirement (?P<old>.+) must use PR #\d+: rename it to (?P<new>.+)$")
_FAIL_DUP = re.compile(r"^\[FAIL\] requirement .+ is defined \d+ times:")
_BUDGET_ERR = re.compile(
    r'^\[ERROR\] budget "(?P<surface>[^"]+)" grows by (?P<delta>\d+) bytes gzip over main '
    r"\((?P<main>\d+) -> (?P<pr>\d+)\) against this PR's allowance of (?P<allowance>\d+) "
    r"\(plus (?P<noise>\d+) B build noise\)"
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*")
_PAYBACK = "none planned; the growth is the feature's own code on this surface"


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd or Path.cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run_git(cwd, *args)


def _charge_date(when: date | None = None) -> str:
    d = when or datetime.now(tz=UTC).date()
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b %Y')}"


def _need_bytes(delta: int, allowance: int) -> int | None:
    over = delta - allowance
    if over <= 0:
        return None
    need = math.ceil(over / 1024) * 1024
    if need - over < 256:
        need += 1024
    return need


def _parse_budget_overs(text: str) -> list[tuple[str, int, int, int, int]]:
    out: list[tuple[str, int, int, int, int]] = []
    for raw in text.splitlines():
        line = _ANSI.sub("", _TS.sub("", raw)).rstrip("\n")
        m = _BUDGET_ERR.match(line)
        if not m:
            continue
        main_b, pr_b = int(m.group("main")), int(m.group("pr"))
        if pr_b <= main_b:
            continue
        delta, allowance = int(m.group("delta")), int(m.group("allowance"))
        if _need_bytes(delta, allowance) is None:
            continue
        out.append((m.group("surface"), main_b, pr_b, delta, allowance))
    return out


def _fetch_frontend_log(head_sha: str) -> str:
    repo = "private_owner/music-dj-tools"
    listed = _run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--commit",
            head_sha,
            "--workflow",
            "ci.yml",
            "--json",
            "databaseId",
            "--limit",
            "1",
        ]
    )
    if listed.returncode != 0 or not listed.stdout.strip():
        raise RuntimeError(f"gh run list failed: {listed.stderr.strip() or listed.stdout}")
    run_id = json.loads(listed.stdout)[0]["databaseId"]
    jobs = _run(["gh", "run", "view", str(run_id), "--repo", repo, "--json", "jobs"])
    if jobs.returncode != 0:
        raise RuntimeError(f"gh run view failed: {jobs.stderr.strip()}")
    job_id = next(
        (
            j["databaseId"]
            for j in json.loads(jobs.stdout).get("jobs", [])
            if j.get("name", "").startswith("frontend unit")
        ),
        None,
    )
    if job_id is None:
        raise RuntimeError("no frontend unit job in CI run")
    logs = _run(["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs"])
    if logs.returncode != 0:
        raise RuntimeError(f"gh api logs failed: {logs.stderr.strip()}")
    return logs.stdout


def _parse_rename_failures(failures: list[str]) -> tuple[list[tuple[str, str]], bool]:
    pairs: list[tuple[str, str]] = []
    dup = False
    for line in failures:
        if _FAIL_DUP.match(line):
            dup = True
        elif m := _FAIL_RENAME.match(line):
            pairs.append((m.group("old"), m.group("new")))
    return pairs, dup


def _rename_targets(pairs: list[tuple[str, str]], paths: list[str], cwd: Path) -> list[str]:
    hits: list[str] = []
    for rel in paths:
        path = cwd / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if any(re.search(rf"\b{re.escape(old)}\b", text) for old, _ in pairs):
            hits.append(rel)
    return hits


def _apply_renames(pairs: list[tuple[str, str]], paths: list[str], cwd: Path) -> list[str]:
    changed: list[str] = []
    for rel in _rename_targets(pairs, paths, cwd):
        path = cwd / rel
        text = path.read_text(encoding="utf-8")
        for old, new in pairs:
            text = re.sub(rf"\b{re.escape(old)}\b", new, text)
        path.write_text(text, encoding="utf-8")
        changed.append(rel)
    return changed


def _apply_charges(
    root: Path,
    pr: int,
    overs: list[tuple[str, int, int, int, int]],
    head_short: str,
    when: date | None,
) -> tuple[list[str], list[str], int]:
    created: list[str] = []
    updated: list[str] = []
    budgets = root / "apps" / "webui" / "frontend" / "budgets"
    charge_date = _charge_date(when)
    for surface, main_b, pr_b, delta, allowance in overs:
        need = _need_bytes(delta, allowance)
        assert need is not None
        surf_dir = budgets / surface
        if not surf_dir.is_dir():
            print(f"[UNKNOWN] budget surface missing: {surface}", file=sys.stderr)
            sys.exit(4)
        charge_rel = f"apps/webui/frontend/budgets/{surface}/charges/{pr}.json"
        charge_path = root / charge_rel
        existed = charge_path.is_file()
        if existed:
            cur = json.loads(charge_path.read_text(encoding="utf-8"))
            if int(cur.get("bytes", 0)) >= need:
                continue
            updated.append(charge_rel)
        else:
            created.append(charge_rel)
        reason = f"PR #{pr} grows the {surface} surface; measured +{delta} B gzip over main; ceiled to KiB."
        payload = {
            "bytes": need,
            "measured_main": main_b,
            "measured_with_pr": pr_b,
            "sha": head_short,
            "date": charge_date,
            "reason": reason,
            "payback": _PAYBACK,
        }
        if existed:
            cur = json.loads(charge_path.read_text(encoding="utf-8"))
            cur.update(payload)
            charge_path.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
        else:
            charge_path.parent.mkdir(parents=True, exist_ok=True)
            charge_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return created, updated, len(created) + len(updated)


def _plan_charges(root: Path, pr: int, overs: list[tuple[str, int, int, int, int]]) -> tuple[list[str], list[str], int]:
    created: list[str] = []
    updated: list[str] = []
    budgets = root / "apps" / "webui" / "frontend" / "budgets"
    for surface, _main, _pr, delta, allowance in overs:
        need = _need_bytes(delta, allowance)
        assert need is not None
        if not (budgets / surface).is_dir():
            print(f"[UNKNOWN] budget surface missing: {surface}", file=sys.stderr)
            sys.exit(4)
        charge_rel = f"apps/webui/frontend/budgets/{surface}/charges/{pr}.json"
        charge_path = root / charge_rel
        if charge_path.is_file():
            if int(json.loads(charge_path.read_text(encoding="utf-8")).get("bytes", 0)) >= need:
                continue
            updated.append(charge_rel)
        else:
            created.append(charge_rel)
    return created, updated, len(created) + len(updated)


def _restore(renamed: list[str], created: list[str], updated: list[str], cwd: Path) -> None:
    for rel in renamed + updated:
        _git(cwd, "checkout", "--", rel)
    for rel in created:
        (cwd / rel).unlink(missing_ok=True)


def _run_checks(root: Path, base_branch: str, pr: int) -> tuple[bool, str]:
    """Format, regenerate and check the repaired WORKING TREE: renames are not staged yet, so HEAD is the unrepaired
    commit and checking it would always reproduce the failure the renames fixed (PR #9928 review, issue #9932)."""
    base_sha = _git(root, "rev-parse", f"origin/{base_branch}").stdout.strip()
    steps: list[tuple[list[str], tuple[int, ...]]] = [
        (["uv", "run", "--no-sync", "python", "-m", "scripts.format_check_changed", "--write"], (0, 1)),
        (["uv", "run", "--no-sync", "python", "-m", "scripts.build_reqs_json"], (0,)),
        (["uv", "run", "--no-sync", "python", "-m", "scripts.format_check_changed"], (0,)),
        (["uv", "run", "--no-sync", "python", "-m", "scripts.build_reqs_json", "--check"], (0,)),
        (
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-m",
                "scripts.req_id_pr_number",
                "--base",
                base_sha,
                "--head-worktree",
                "--pr",
                str(pr),
            ],
            (0,),
        ),
    ]
    for argv, ok in steps:
        proc = _run(argv, cwd=root)
        if proc.returncode not in ok:
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
            return False, "\n".join(tail)
    return True, ""


def _plan_renames(
    cwd: Path,
    args: argparse.Namespace,
    base_text: str,
    base_rev: str,
) -> tuple[list[tuple[str, str]], list[str], int | None]:
    head_text = (cwd / SOURCE).read_text(encoding="utf-8")
    carried_text, _ = _show("git", "HEAD", CARRIED)
    failures = check(defined_ids(base_text), defined_ids(head_text), args.pr, carried_ids(carried_text or ""))
    pairs, has_dup = _parse_rename_failures(failures)
    if has_dup:
        for line in failures:
            if _FAIL_DUP.match(line):
                print(line, file=sys.stderr)
        print(f"NOTHING {args.pr} (duplicate requirement definitions are not mechanical)", file=sys.stderr)
        return [], [], 3
    for old, _ in pairs:
        if re.search(rf"\b{re.escape(old)}\b", base_text):
            print(f"[skip] {old} already appears on {base_rev} {SOURCE}; human decision", file=sys.stderr)
            return [], [], 3

    diff_proc = _git(cwd, "diff", "--name-only", f"origin/{args.base}...HEAD")
    if diff_proc.returncode != 0:
        print(diff_proc.stderr.strip() or "git diff failed", file=sys.stderr)
        return [], [], 4
    diff_paths = [p for p in diff_proc.stdout.splitlines() if p.strip()]
    return pairs, diff_paths, None


def _load_frontend_log(args: argparse.Namespace, head_sha: str) -> tuple[str, int | None]:
    try:
        log_text = (
            args.frontend_log.read_text(encoding="utf-8")
            if args.frontend_log is not None
            else _fetch_frontend_log(head_sha)
        )
    except RuntimeError as exc:
        print(f"[UNKNOWN] {exc}", file=sys.stderr)
        return "", 4
    return log_text, None


def _finish(
    cwd: Path,
    args: argparse.Namespace,
    pairs: list[tuple[str, str]],
    diff_paths: list[str],
    overs: list[tuple[str, int, int, int, int]],
    head_short: str,
    n_renames: int,
    n_charges: int,
) -> int:
    renamed = _apply_renames(pairs, diff_paths, cwd) if pairs else []
    created_charges: list[str] = []
    updated_charges: list[str] = []
    if overs:
        created_charges, updated_charges, n_charges = _apply_charges(cwd, args.pr, overs, head_short, None)

    ok, err = _run_checks(cwd, args.base, args.pr)
    if not ok:
        _restore(renamed, created_charges, updated_charges, cwd)
        print(err, file=sys.stderr)
        return 2

    proc = _git(cwd, "status", "--porcelain")
    paths = [line[3:].strip().strip('"') for line in proc.stdout.splitlines() if len(line) >= 4]
    for rel in paths:
        _git(cwd, "add", "--", rel)
    print(f"FIXED {args.pr} renames={n_renames} charges={n_charges} files={len(paths)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--frontend-log", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    cwd = Path.cwd()

    head_sha = _git(cwd, "rev-parse", "HEAD").stdout.strip()
    head_short = head_sha[:10]
    base_rev = f"origin/{args.base}"
    base_text, base_err = _show("git", base_rev, SOURCE)
    if base_text is None:
        print(f"[UNKNOWN] cannot read {SOURCE} at {base_rev}: {base_err}", file=sys.stderr)
        return 4

    pairs, diff_paths, stop = _plan_renames(cwd, args, base_text, base_rev)
    if stop is not None:
        return stop

    log_text, stop = _load_frontend_log(args, head_sha)
    if stop is not None:
        return stop

    overs = _parse_budget_overs(log_text)
    created_charges, updated_charges, n_charges = _plan_charges(cwd, args.pr, overs) if overs else ([], [], 0)
    rename_files = _rename_targets(pairs, diff_paths, cwd) if pairs else []
    if not (rename_files or created_charges or updated_charges):
        print(f"NOTHING {args.pr}")
        return 3

    n_renames = len(pairs)
    if args.dry_run:
        print(f"WOULD-FIX {args.pr} renames={n_renames} charges={n_charges} files={len(rename_files) + n_charges}")
        return 0

    return _finish(cwd, args, pairs, diff_paths, overs, head_short, n_renames, n_charges)


if __name__ == "__main__":
    sys.exit(main())
