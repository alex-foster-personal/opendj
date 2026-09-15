"""Whether a PR's OWN `.planning/debt/<pr>.md` is well formed (issue #2980).

Split into its own module from the start, on the same 600-line seam
`review_ledger.py` and `review_thread_refs.py` already split
`scripts/review_thread_triage.py` on: this answers one self-contained
question -- is THIS PR's debt file well formed, and does the same directory
hold an unrelated main-side defect -- while the caller decides what to do
with the verdict.

Reuses `scripts.debt_index`'s parser (`check_pr_debt_file`,
`other_debt_file_problems`) rather than a second copy of the format rules,
per the issue's requirement.
"""

from __future__ import annotations

from pathlib import Path

from scripts import debt_index
from scripts.review_ledger import OWNER, REPO, debt_text_at


def verdict_from_text(
    number: int, text: str, debt_dir: Path = debt_index.DEBT_DIR
) -> tuple[str | None, list[str]]:
    """(this PR's own format error, or None; sibling main-side problems),
    given TEXT ALREADY IN HAND. This is the pure core `check_pr_head_debt_file`
    fetches for, split out so a test can drive it with a fixture instead of a
    network call. A PR with no debt file (`text == ""`) returns `(None, [])`,
    unchanged from before this check existed.

    The sibling scan (`debt_dir`, overridable for the same testing reason)
    runs only once this PR's OWN file is confirmed well formed: a PR whose
    own file is already broken has one problem to fix, not two unrelated ones
    printed together, and this ordering is what keeps a pre-existing
    main-side defect from ever being blamed on a passing PR.
    """
    if not text:
        return None, []
    try:
        debt_index.check_pr_debt_file(number, text)
    except ValueError as exc:
        return str(exc), []
    return None, debt_index.other_debt_file_problems(number, debt_dir)


def check_pr_head_debt_file(
    number: int, head_sha: str, owner: str = OWNER, repo: str = REPO
) -> tuple[str | None, list[str]]:
    """`verdict_from_text`, fed by fetching `.planning/debt/<number>.md` at
    the PR's own pinned head -- the same read `_debt_permalinks` performs for
    the ledger check, and the same reason: a local checkout could belong to
    any branch, or none.
    """
    text = debt_text_at(number, head_sha, owner=owner, repo=repo)
    return verdict_from_text(number, text)


def render_debt_verdict(
    number: int, head_sha: str, error: str | None, main_side: list[str]
) -> str:
    """The report block `review_thread_triage` prints for a debt verdict, or
    `""` when there is nothing to say -- kept out of the caller so its own
    file stays under the repo's 600-line file-size ratchet."""
    if error:
        return f"DEBT FILE INVALID (.planning/debt/{number}.md at {head_sha}): {error}"
    if main_side:
        lines = ["main-side debt file problem(s) (pre-existing on main, not this PR's):"]
        lines += [f"  {problem}" for problem in main_side]
        return "\n".join(lines)
    return ""
