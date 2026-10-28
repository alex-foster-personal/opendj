"""File every non-blocking review thread a PR merges past as ONE follow-up issue (REVIEW-22).

Since ADR-0124 (Wed 28 Oct 2026) review-triage blocks only on a P0 or a human BLOCKING
thread. Every other open bot thread -- P1, P2, P3, bot BLOCKING, or a debt-log reply the
PR's debt file does not hold -- would otherwise be lost at merge, so triage writes them
here, at triage time, before it returns a pass.

Why an issue and not `(private path)/<pr>.md`: the debt file lives on the PR branch, so
writing it means a push, which moves the head the nucbox trunk-bridge pinned its verdict
to and races the author's own pushes. An issue is written without touching the branch.

One issue per PR, found again by its label plus the marker in its body, never by search
(the search index lags, which would file duplicates). The issue is keyed on its SET of
thread permalinks: a run whose set matches writes nothing, a ticked box survives a
rewrite, and a closed issue is reopened only when a thread it did not list appears.

Requirements (mini-PRD):
  / Every follow-up thread at the head appears in the issue body with its permalink.
    [if] a non-blocking unterminated thread is absent from the body [then] broken
  / A PR with no follow-ups creates no issue.
    [if] a clean PR files an empty issue [then] broken
  / A second run finds the first run's issue instead of filing another; two racing runs
    that both created one leave the oldest open and close the other as a duplicate.
    [if] two runs on one PR leave two open issues [then] broken
  / A filing that fails raises TriageError, which triage reports as UNKNOWN (exit 3).
    [if] a gh failure leaves triage passing with the threads unfiled [then] broken
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from scripts.review_gh import TriageError, _gh, _paginated_json_list
from scripts.review_thread_parse import Thread

LABEL = "review-followup"
_ROW = re.compile(r"^- \[(?P<tick>[ xX])\] .*\n  (?P<link>\S+)$", re.MULTILINE)


def marker(number: int) -> str:
    return f"<!-- review-followups pr={number} -->"


def title(number: int) -> str:
    return f"Review follow-ups for PR #{number}"


def ticked_links(body: str) -> dict[str, bool]:
    """Pure: permalink -> whether its box is ticked, read back from a rendered body."""
    return {m["link"]: m["tick"] != " " for m in _ROW.finditer(body.replace("\r\n", "\n"))}


def render_body(number: int, threads: Sequence[Thread], ticked: Mapping[str, bool] | None = None) -> str:
    """Pure: the issue body for these threads, deterministic for a given input; `ticked` keeps boxes."""
    ticked = ticked or {}
    rows = []
    for t in sorted(threads, key=lambda t: (t.permalink, t.path, t.line or 0, t.summary)):
        where = f"{t.path}:{t.line}" if t.line else t.path
        box = "x" if ticked.get(t.permalink) else " "
        rows.append(f"- [{box}] [{t.severity}/{t.blocking}] `{where}` ({t.bot}): {t.summary}\n  {t.permalink}")
    return "\n".join(
        [
            marker(number),
            f"Non-blocking review threads left open when PR #{number} passed review-triage "
            "(REVIEW-22, ADR-0124). Fix forward in a later PR, or reply on the thread and resolve "
            "it; tick each box as it is done and close this issue when all are.",
            "",
            *rows,
        ]
    )


def _issues(owner: str, repo: str, number: int) -> list[dict]:
    issues = _paginated_json_list(f"repos/{owner}/{repo}/issues?labels={LABEL}&state=all&per_page=100")
    return sorted((i for i in issues if marker(number) in (i.get("body") or "")), key=lambda i: i["number"])


def _keep_oldest(owner: str, repo: str, found: list[dict]) -> dict | None:
    """Two racing runs can each create one: keep the oldest, close the rest as duplicates."""
    for dup in found[1:]:
        if dup.get("state") == "open":
            _gh(["api", f"repos/{owner}/{repo}/issues/{dup['number']}", "--method", "PATCH", "--input", "-"],
                {"state": "closed", "state_reason": "duplicate"})
    return found[0] if found else None


def file_follow_ups(owner: str, repo: str, number: int, threads: Sequence[Thread]) -> str | None:
    """Create or refresh the PR's follow-up issue; return its URL, or None when there is nothing to file."""
    if not threads:
        return None
    issue = _keep_oldest(owner, repo, _issues(owner, repo, number))
    if issue is None:
        payload = {"title": title(number), "body": render_body(number, threads), "labels": [LABEL]}
        _gh(["api", f"repos/{owner}/{repo}/issues", "--method", "POST", "--input", "-"], payload)
        issue = _keep_oldest(owner, repo, _issues(owner, repo, number))  # re-read: a racer may have filed too
        if issue is None:
            raise TriageError(f"created the PR #{number} follow-up issue but cannot read it back by label and marker")
    # Reconcile whichever issue was kept, ours or a racer's, to this run's threads.
    ticked = ticked_links(issue.get("body") or "")
    links = {t.permalink for t in threads}
    if links != set(ticked):
        patch: dict[str, str] = {"body": render_body(number, threads, ticked)}
        if links - set(ticked):  # a thread the issue never listed: someone has new work
            patch["state"] = "open"
        _gh(["api", f"repos/{owner}/{repo}/issues/{issue['number']}", "--method", "PATCH", "--input", "-"], patch)
    return issue["html_url"]
