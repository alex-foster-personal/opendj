"""Turn comment pins green when the PR that fixed them merges (#858).

Usage::

    just pin-merged 890                    # mark every reachable daemon, live
    just pin-merged 890 --dry-run          # print what it would do
    just pin-merged 890 --url http://127.0.0.1:8728  # plus an extra target

A merged PR's pins are not all on one daemon: the maintainer's live pins are spread
across worktree dev ports, ad-hoc audition servers, and the installed app
(local and silver), exactly as documented in feedback_harvest.py's target
discovery. Patching only nucbox's own loopback engine (issue #914 review,
Wed 2 Sep 2026) marks nucbox's copy and leaves every other daemon holding
the same pin amber forever, so this reuses that same `_discover()` rather
than assuming a single fixed URL.

The issue asks for polling as the fallback so "a pin never stays amber because
a worker forgot". This is the deliberate KISS half of that: no poller, no
GitHub API round trip per pin, no background job. The merge lane already knows
the PR number at the moment it merges, and the PR body already says which pins
it carried, so the merge lane calls this once and every pin it names goes
green in the same second. A poller is strictly more machinery for information
we are handed for free; if pins do start going stale, THAT is the evidence
that earns one.

Two shapes are read out of the PR body, because the maintainer's pins today carry both:

    pin <pin id> -> <commit sha>     explicit, and carries the SHA
    Fixes #888 / Closes #876 / ...   a closing keyword, so that issue closes
                                     with this merge; every pin whose
                                     issue_url is that issue is merged too
    Fixes other/project#888         same, using GitHub's owner/repo shorthand
                                     for a cross-repo closing reference

A bare ``#999`` mention is NOT a closing reference and is ignored: GitHub only
closes on the keyword, so acting on a mention would turn a pin green on work
that has not landed.

Pins already ``merged`` or ``archived`` are skipped, so re-running is a no-op
rather than a second note. The merge note is APPENDED to whatever the fixing
agent wrote; that explanation is the most valuable thing on the pin and must
not be overwritten by bookkeeping.

Requirements (mini-PRD):
- ✔︎ 🎯 parse_pr_body: all three reference shapes (pin-sha, full URL, and the
  owner/repo shorthand), closing keywords only.
  [if] a bare #999 mention turns a pin green [then ⛔️]
  [if] "Fixes other/project#888" is not read as closing other/project#888 [then ⛔️]
- ✔︎ 🎯 select_merged_pins: id match or issue match, no substring matches,
  already-merged pins skipped.
  [if] /issues/88 is closed by "Fixes #888" [then ⛔️]
- ✔︎ 🎯 merged_note: appends, idempotent.
  [if] a second run doubles the note [then ⛔️]
- ✔︎ ✅ the PATCH half is a thin shell over those three, exercised live by the
  merge lane rather than by a test that would have to drive a real engine.
- ✔︎ an unreadable daemon after discovery fails the run (exit 1) and names the
  daemon; other daemons are still marked.
  [if] a GET failure exits 0 [then ⛔️]
- ✔︎ 🎯 a configured daemon (--url or worktree dev port) that discovery cannot
  reach fails the run (exit 1), even when another daemon was marked.
  [if] a worktree dev port drops out of _discover() and a second daemon is
  still reachable [then ⛔️ the run exits 0 with the dropped daemon's pins
  amber]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.error
from dataclasses import dataclass, field

try:
    from scripts.feedback_harvest import Target, _discover, _env_ports
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.pin_mark_merged") from None
    raise

# `pin <id> -> <sha>`: ids are 12 hex chars today, shas 7-40. Both bounded so
# a prose line mentioning the word "pin" cannot be read as a reference.
_PIN_SHA = re.compile(r"^\s*pin\s+([0-9a-f]{6,32})\s*->\s*([0-9a-f]{7,40})\s*$", re.M | re.I)
# GitHub's own closing keywords, and only those. A full issue URL and the
# `owner/repo#N` shorthand both carry their own repo; a bare `#N` is
# unambiguous by GitHub's own semantics - it can only close an issue in the
# SAME repo as the PR.
_CLOSES = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[\s:]+"
    r"(?:https?://\S*?/([\w.-]+/[\w.-]+)/issues/(\d+)"
    r"|([\w.-]+/[\w.-]+)#(\d+)"
    r"|#(\d+))",
    re.I,
)

# A pin's issue_url, to recover the repo a full-URL closing reference must
# match. Mirrors _CLOSES' URL half so both read "owner/repo" the same way.
_ISSUE_URL = re.compile(r"([\w.-]+/[\w.-]+)/issues/(\d+)$")


@dataclass(frozen=True)
class PinRefs:
    """What a merged PR body says it carried."""

    pin_shas: dict[str, str] = field(default_factory=dict)
    issues: set[int] = field(default_factory=set)
    # (owner/repo, issue number) pulled from a full issue URL closing
    # reference - kept separate from `issues` because a bare number from one
    # repo must never match a same-numbered issue in another.
    repo_issues: set[tuple[str, int]] = field(default_factory=set)


@dataclass(frozen=True)
class MergeTarget:
    id: str
    sha: str | None
    reason: str


def parse_pr_body(body: str | None) -> PinRefs:
    if not body:
        return PinRefs()
    pin_shas = {m.group(1).lower(): m.group(2).lower() for m in _PIN_SHA.finditer(body)}
    issues: set[int] = set()
    repo_issues: set[tuple[str, int]] = set()
    for url_repo, url_n, short_repo, short_n, hash_n in _CLOSES.findall(body):
        if url_repo and url_n:
            repo_issues.add((url_repo.lower(), int(url_n)))
        elif short_repo and short_n:
            repo_issues.add((short_repo.lower(), int(short_n)))
        elif hash_n:
            issues.add(int(hash_n))
    return PinRefs(pin_shas=pin_shas, issues=issues, repo_issues=repo_issues)


def select_merged_pins(comments: list[dict], refs: PinRefs, this_repo: str) -> list[MergeTarget]:
    """Which pins this merge turns green, in board order, each named once.

    ``this_repo`` is the ``owner/repo`` the PR itself lives in. A bare ``#N``
    closing reference is unambiguous by GitHub's own semantics - it can only
    close an issue in that SAME repo - so ``refs.issues`` may only match a
    pin whose own ``issue_url`` also points at ``this_repo``; a pin filed
    against a different repository must never match a same-numbered issue
    it does not actually reference.
    """
    this_repo = this_repo.lower()
    targets: list[MergeTarget] = []
    for comment in comments:
        pin_id = str(comment.get("id", ""))
        if comment.get("status") in {"merged", "archived"}:
            continue
        if pin_id in refs.pin_shas:
            targets.append(MergeTarget(pin_id, refs.pin_shas[pin_id], "listed in the PR body"))
            continue
        issue_url = comment.get("issue_url")
        if not issue_url:
            continue
        # Match the WHOLE trailing number: /issues/88 is not closed by #888.
        match = _ISSUE_URL.search(str(issue_url).rstrip("/"))
        if not match:
            continue
        repo, number = match.group(1).lower(), int(match.group(2))
        if (repo == this_repo and number in refs.issues) or (repo, number) in refs.repo_issues:
            targets.append(MergeTarget(pin_id, None, f"closes #{number}"))
    return targets


def merged_note(existing: str | None, pr_number: int, sha: str | None) -> str:
    line = f"Merged in PR #{pr_number}" + (f" ({sha})" if sha else "")
    if existing and line in existing:
        return existing
    return f"{existing}\n\n{line}" if existing else line


def _configured_bases(extra_urls: list[str]) -> list[str]:
    """Daemons the operator named (worktree dev port, explicit --url): a miss
    is a failure, not a skip."""
    return [f"http://127.0.0.1:{port}" for port in _env_ports()] + [
        u.rstrip("/") for u in extra_urls
    ]


# ----- the shell around them ---------------------------------------------
def _gh_pr_body(pr_number: int) -> str:
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", "body,state", "-q", ".body"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _gh_repo() -> str:
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _mark_on_daemon(
    daemon: Target, pr_number: int, refs: PinRefs, this_repo: str, dry_run: bool
) -> int:
    """Mark every pin this PR merged on ONE daemon. Returns how many it marked."""
    comments = daemon.request("GET", "/api/v1/feedback/comments")["comments"]

    marks = select_merged_pins(comments, refs, this_repo)
    if not marks:
        return 0

    by_id = {c["id"]: c for c in comments}
    for mark in marks:
        payload = {
            "status": "merged",
            "agent_note": merged_note(by_id[mark.id].get("agent_note"), pr_number, mark.sha),
        }
        if mark.sha:
            payload["fixed_in_sha"] = mark.sha
        if dry_run:
            print(f"[DRY] {daemon.machine} {mark.id} -> merged ({mark.reason})")
            continue
        daemon.request("PATCH", f"/api/v1/feedback/comments/{mark.id}", payload)
        print(f"[OK] {daemon.machine} {mark.id} -> merged ({mark.reason})")
    return len(marks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pr_number", type=int, help="the MERGED pull request number")
    parser.add_argument(
        "--url", action="append", default=[], help="extra daemon, beside the auto-discovered ones"
    )
    parser.add_argument("--skip-silver", action="store_true", help="do not reach silver via ssh")
    parser.add_argument("--body", help="read the PR body from this file instead of gh")
    parser.add_argument("--dry-run", action="store_true", help="print, patch nothing")
    args = parser.parse_args(argv)

    if args.body:
        with open(args.body, encoding="utf-8") as f:
            body = f.read()
    else:
        body = _gh_pr_body(args.pr_number)
    refs = parse_pr_body(body)
    if not refs.pin_shas and not refs.issues and not refs.repo_issues:
        print(f"[WARN] PR #{args.pr_number} body names no pin and closes no issue; nothing to mark")
        return 0

    daemons = _discover(args.url, args.skip_silver)
    if not daemons:
        print("[ERROR] no reachable feedback daemon on any target; nothing marked")
        return 1

    reachable = {d.base for d in daemons}
    unreachable = [base for base in _configured_bases(args.url) if base not in reachable]
    for base in unreachable:
        print(f"[ERROR] configured daemon {base} unreachable, its pins NOT marked")

    this_repo = _gh_repo()
    failed: list[Target] = []
    marked = 0
    for d in daemons:
        try:
            marked += _mark_on_daemon(d, args.pr_number, refs, this_repo, args.dry_run)
        except (urllib.error.URLError, OSError) as exc:
            print(f"[ERROR] {d.machine} {d.base} unreadable, pins NOT marked: {exc}")
            failed.append(d)

    if failed or unreachable:
        n = len(failed) + len(unreachable)
        print(
            f"[ERROR] {n} daemon(s) unreachable or unreadable; "
            f"re-run: just pin-merged {args.pr_number}"
        )
        return 1
    if not marked:
        print(f"[OK] PR #{args.pr_number} matched no open pin on any reachable daemon")
    return 0


if __name__ == "__main__":
    sys.exit(main())
