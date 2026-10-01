"""A PR's unified diff for the review tooling: from GitHub, or from the checkout
when GitHub refuses the size.

GitHub renders no diff over 300 files and answers the diff media type with
HTTP 406. `just review-triage` and every CLI review lane read the diff through
`scripts.review_lane.diff_of`, so a PR that large could not be measured at all
(exit 3 on PR #3837, Thu 1 Oct 2026). `scripts/perf/perfbatch_gate.py` met the
same refusal and answers it the same way.

Requirements (mini-PRD):
  / On the size refusal, read the same merge-base-to-head diff from the checkout.
    [if a 406 on an oversized PR still raises then broken]
  / Any other gh failure is still a TriageError, never a local guess.
    [if an HTTP 502 falls back to the checkout then broken]
    [if a 406 for any other reason falls back to the checkout then broken]
  / Bytes that are not UTF-8 are a TriageError, never a diff with characters replaced.
    [if a changed latin-1 byte reaches a reviewer as U+FFFD then broken]
  / A checkout that lacks either commit is a TriageError, never a partial diff.
    [if a missing head commit yields an empty or wrong diff then broken]
  / The local diff is shaped like GitHub's regardless of the caller's git config.
    [if diff.noprefix in a user's config changes the headers then broken]
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.review_gh import TriageError, _gh

REPO_ROOT = Path(__file__).resolve().parents[1]

#: What gh reports when GitHub refuses to render a diff for its size: the reason
#: AND the status, as GitHub returned them for PR #3837 on Thu 1 Oct 2026. It is
#: the one failure the checkout can answer instead; a 406 for any other reason
#: (a media type GitHub does not serve, say) is not it.
DIFF_TOO_LARGE: tuple[str, ...] = ("the diff exceeded the maximum number of files", "(HTTP 406)")

#: Pin the output shape: `a/` and `b/` prefixes, rename detection, no color, no
#: external or textconv driver. A caller's own git config must not be able to
#: change what `anchorable_lines` and `split_generated_data` parse.
_GIT_DIFF: tuple[str, ...] = (
    "git",
    "-c",
    "core.quotePath=true",
    "diff",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--find-renames",
)


def pr_diff(repo: str, pr: str, repo_root: Path = REPO_ROOT) -> str:
    """The PR's unified diff. Only the size refusal reaches the checkout."""
    try:
        return _gh(["api", f"repos/{repo}/pulls/{pr}", "-H", "Accept: application/vnd.github.v3.diff"])
    except TriageError as exc:
        if not all(part in str(exc) for part in DIFF_TOO_LARGE):
            raise
    head, merge_base = _diff_endpoints(repo, pr)
    return checkout_diff(merge_base, head, repo_root)


def _diff_endpoints(repo: str, pr: str) -> tuple[str, str]:
    """(head, merge base) of the PR, both from GitHub.

    The PR object's `baseRefOid` is the base branch tip, not the point the
    diff is taken from, so the compare API is asked for the merge base. It is
    not computed locally: a shallow checkout answers `git merge-base` with a
    wrong commit or none, and this repo's clones are shallow.
    """
    view = json.loads(_gh(["pr", "view", pr, "--repo", repo, "--json", "headRefOid,baseRefOid"]))
    head, base = view.get("headRefOid"), view.get("baseRefOid")
    if not head or not base:
        raise TriageError(f"PR #{pr} reported no head or base commit: {view}")
    compare = f"repos/{repo}/compare/{base}...{head}"
    merge_base = _gh(["api", compare, "--jq", ".merge_base_commit.sha"]).strip()
    if not merge_base:
        raise TriageError(f"compare API returned no merge base for {compare}")
    return head, merge_base


def checkout_diff(merge_base: str, head: str, repo_root: Path = REPO_ROOT) -> str:
    """The diff between two commits, read from this checkout.

    Read as bytes and decoded without newline translation: text mode would
    turn a bare CR inside a diff line into a line break, and
    `split_generated_data` splits on LF alone for exactly that reason.
    """
    for sha in (merge_base, head):
        present = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        if present.returncode != 0:
            raise TriageError(
                f"commit {sha[:12]} is not in the checkout at {repo_root}; fetch the PR head and its base, then re-run"
            )
    proc = subprocess.run([*_GIT_DIFF, merge_base, head], cwd=repo_root, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise TriageError(f"git diff {merge_base[:12]} {head[:12]} failed: {detail or '<no stderr>'}")
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TriageError(
            f"git diff {merge_base[:12]} {head[:12]} is not valid UTF-8 at byte {exc.start}; "
            "a review of replaced characters would not be a review of the commit"
        ) from exc
