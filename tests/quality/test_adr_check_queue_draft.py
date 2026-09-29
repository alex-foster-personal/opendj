"""What adr-check.yml runs on a Trunk queue draft versus an ordinary PR (PR #4227).

A `trunk-merge/*` draft has no PR body, so body validation cannot apply to it.
Its tree, though, is exactly the tree that lands on main, so the merged-tree
duplicate-id check must still run there. These tests read the workflow's own
step conditions and arguments, then execute those arguments against real git
repositories in tmp_path.

Regression lines:
  - if a trunk-merge/* draft skips the merged-tree ADR check then a duplicate ADR id lands on main
  - if an ordinary PR stops getting body validation then a gated change merges with no ADR line

-Claude
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import adr_check

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / ".github" / "workflows" / "adr-check.yml"
GATE_MODULE = "scripts.adr_check"
PR_NUMBER_EXPR = "${{ github.event.pull_request.number }}"
# The only step/job conditions this model understands; any other form fails loud.
CONDITION_RUNS_ON_DRAFT = {
    "${{ startsWith(github.head_ref, 'trunk-merge/') }}": True,
    "${{ !startsWith(github.head_ref, 'trunk-merge/') }}": False,
}
QUEUE_DRAFT_REF = "trunk-merge/pr-4227/0b1c2d3e"
ORDINARY_PR_REF = "af--some-feature"
ADR_DIR = "docs/decisions"
GATED_PATH = "apps/cloud/service.py"


# -----------------------------------------------------------------------------
def _condition_holds(condition: str | None, head_ref: str) -> bool:
    if condition is None:
        return True
    if condition not in CONDITION_RUNS_ON_DRAFT:
        raise AssertionError(f"unmodelled condition in {GATE.name}: {condition!r}")
    is_draft = head_ref.startswith("trunk-merge/")
    return is_draft == CONDITION_RUNS_ON_DRAFT[condition]


def _gate_argvs_for(head_ref: str) -> list[list[str]]:
    """The scripts.adr_check argument lists the workflow would run for this head ref."""
    job = yaml.safe_load(GATE.read_text())["jobs"]["gate"]
    if not _condition_holds(job.get("if"), head_ref):
        return []
    argvs: list[list[str]] = []
    for step in job["steps"]:
        run = str(step.get("run", ""))
        if GATE_MODULE not in run or not _condition_holds(step.get("if"), head_ref):
            continue
        tokens = shlex.split(run.replace(PR_NUMBER_EXPR, "4227"))
        argvs.append(tokens[tokens.index(GATE_MODULE) + 1 :])
    return argvs


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", f"touch {rel}")
    return _git(root, "rev-parse", "HEAD")


def _adr(number: str, slug: str) -> tuple[str, str]:
    body = f"# ADR-{number}: {slug}\n\nStatus: accepted\nDate: Tue 29 Sep 2026\n"
    return f"{ADR_DIR}/ADR-{number}-{slug}.md", body


def _repo_with_batch(tmp_path: Path, batch_adr: tuple[str, str]) -> Path:
    """main carries ADR-0002-main; the checked-out batch head adds `batch_adr` on top."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _commit(repo, *_adr("0001", "base"))
    main_tip = _commit(repo, *_adr("0002", "main"))
    _git(repo, "update-ref", "refs/remotes/origin/main", main_tip)
    _git(repo, "checkout", "-q", "-b", "batch")
    _commit(repo, *batch_adr)
    return repo


def _run_gate(argv: list[str], repo: Path, **kwargs) -> int:
    return adr_check.main(argv, repo_root=repo, adr_dir=repo / ADR_DIR, **kwargs)


# -----------------------------------------------------------------------------
def test_queue_draft_runs_exactly_the_merged_tree_check() -> None:
    argvs = _gate_argvs_for(QUEUE_DRAFT_REF)
    assert len(argvs) == 1, f"a queue draft must run one ADR check, got {argvs}"
    assert "--merge-base" in argvs[0], f"a queue draft must check the merged tree: {argvs[0]}"
    assert "--pr" not in argvs[0], "a queue draft has no PR body to validate"


def test_queue_draft_catches_a_duplicate_adr_id_in_the_batch_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (argv,) = _gate_argvs_for(QUEUE_DRAFT_REF)
    repo = _repo_with_batch(tmp_path, _adr("0002", "batch"))
    assert _run_gate(argv, repo) == 1
    err = capsys.readouterr().err
    assert "duplicate ADR id" in err and "ADR-0002" in err, err


def test_queue_draft_with_a_clean_batch_tree_passes(tmp_path: Path) -> None:
    """Control: the red above is the duplicate id, not a broken invocation."""
    (argv,) = _gate_argvs_for(QUEUE_DRAFT_REF)
    assert _run_gate(argv, _repo_with_batch(tmp_path, _adr("0003", "batch"))) == 0


def test_ordinary_pr_runs_exactly_the_body_validation() -> None:
    argvs = _gate_argvs_for(ORDINARY_PR_REF)
    assert len(argvs) == 1, f"an ordinary PR must run one ADR check, got {argvs}"
    assert argvs[0][:2] == ["--pr", "4227"], f"an ordinary PR must validate its body: {argvs[0]}"
    assert "--merge-base" not in argvs[0]


@pytest.mark.parametrize(
    ("body", "expected_rc"),
    [("A gated change with no declaration.", 1), ("ADR: none, because a test fixture.", 0)],
)
def test_ordinary_pr_body_validation_still_bites(
    tmp_path: Path, body: str, expected_rc: int
) -> None:
    (argv,) = _gate_argvs_for(ORDINARY_PR_REF)
    repo = _repo_with_batch(tmp_path, _adr("0003", "feature"))
    rc = _run_gate(argv, repo, fetch=lambda _pr: {"body": body}, changed=[GATED_PATH])
    assert rc == expected_rc
