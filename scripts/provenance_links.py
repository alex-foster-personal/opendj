"""Branch and PR context for harvested prompts.

Answers "what was being worked on when this was typed" by matching a prompt's
timestamp against each branch's active window and attaching the PR that branch
became. Separate from the harvesters and from the ledger writer because it is
the only part that reaches OUT of the machine, to `gh`, and it degrades to an
empty map rather than failing the sweep when that call does not land.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from scripts.provenance_sources import _git

# The output must stay a superset of the ledger committed at HEAD and at the
# trunk. HEAD catches a rebase that dropped a sweep commit; the trunk catches a
# branch cut before one, which HEAD alone cannot see.
TRUNK_BRANCH = "main"
TRUNK_FALLBACK = f"origin/{TRUNK_BRANCH}"
def branch_windows(repo: Path) -> list[tuple[str, float, float]]:
    """(branch, first-commit-epoch, last-commit-epoch) for every local branch.

    NOT `git log --reverse -1`: git applies the -1 limit BEFORE reversing, so
    that returns the NEWEST commit and every window collapses to an instant.
    Take the whole timestamp column once and read both ends off it.

    The window is bounded to commits not on the trunk, so a long-lived branch
    does not swallow every prompt ever sent.
    """
    trunk = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").strip()
    trunk = trunk or TRUNK_FALLBACK
    windows: list[tuple[str, float, float]] = []
    for br in _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split():
        stamps = _git(repo, "log", "--format=%ct", f"{trunk}..{br}").split()
        if not stamps:  # fully merged or empty: fall back to its own tip
            stamps = _git(repo, "log", "--format=%ct", "-1", br).split()
        if not stamps:
            continue
        vals = [float(s) for s in stamps]
        windows.append((br, min(vals), max(vals)))
    return windows
class GhUnavailable(RuntimeError):
    """`gh` is not on PATH at all, so no branch can be linked to its PR.

    Distinct from `gh` running and answering badly, which already degrades to
    an empty map: that is a measurement (this repo has no PRs visible to this
    checkout). An ABSENT `gh` measured nothing, and the two must not look the
    same to a caller deciding whether to advance a watermark. Codex found the
    crash this replaces on #708: the harvest reached `pr_map` only when it had
    prompts, so every test hit the empty path and the exception went to the
    Stop hook's /dev/null every turn.
    """


def pr_map(repo: Path) -> dict[str, dict]:
    try:
        p = subprocess.run(
            ("gh", "pr", "list", "--state", "all", "--limit", "300",
             "--json", "number,headRefName,state,url,title"),
            capture_output=True, text=True, cwd=str(repo), check=False,
        )
    except FileNotFoundError as exc:
        raise GhUnavailable(
            "`gh` is not on PATH, so no harvested prompt can be linked to the "
            "PR it belongs to"
        ) from exc
    if p.returncode != 0:
        return {}
    try:
        return {x["headRefName"]: x for x in json.loads(p.stdout)}
    except json.JSONDecodeError:
        return {}
def attach_links(prompts: list[dict], windows, prs: dict[str, dict]) -> None:
    for pr in prompts:
        if not pr.get("at"):
            continue
        try:
            t = datetime.fromisoformat(pr["at"]).timestamp()
        except ValueError:
            continue
        hits = [b for b, a, z in windows if a <= t <= z]
        if not hits:
            continue
        pr["branches"] = hits[:4]
        linked = [prs[b] for b in hits if b in prs]
        if linked:
            pr["prs"] = [{"number": x["number"], "state": x["state"], "url": x["url"]}
                         for x in linked[:4]]
