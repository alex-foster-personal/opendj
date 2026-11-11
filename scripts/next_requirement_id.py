"""Next free requirement id for a prefix (issue #8634).

Considers ids defined on ``origin/main`` at ``.planning/REQUIREMENTS.md`` (and its
fragments) and ids added by open PRs to those files. One ``gh pr list`` names the open PRs
and their head SHAs (``MAX_GH_CALLS``); each PR's diff is read from git after fetching
``refs/pull/<N>/head``, so a run spends one gh call however many PRs are open. Does not fill gaps; returns max+1 with at
least two digits (``OPS-47``), or ``{prefix}-01`` when the prefix has no numeric
ids yet.

The caller must keep ``origin/main`` current; this module fetches only PR heads it lacks.

    python -m scripts.next_requirement_id OPS
    python -m scripts.next_requirement_id OPS --open-prs-json fixture.json --main-text main.md
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.build_reqs_json import _CODE, _REQ_ID, FRAGMENTS_REL, fragments_text_at, ids_from_requirements_text
from scripts.gh_pr_view import DEFAULT_REPO

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = ".planning/REQUIREMENTS.md"
_MAIN_REF = "origin/main"
_PREFIX_RE = re.compile(rf"^{_CODE}$")
_NUMBERED = re.compile(r"^(?P<code>.+)-(?P<num>\d+)(?P<suffix>[a-z]?)$")
_REQ_MD_MARKERS = (
    "a/.planning/REQUIREMENTS.md",
    "b/.planning/REQUIREMENTS.md",
    f"b/{FRAGMENTS_REL}/",
)


def next_requirement_id(prefix: str, main_ids: set[str], claimed_ids: set[str]) -> str:
    """Return the next free id for ``prefix`` over ``main_ids | claimed_ids``."""
    if not _PREFIX_RE.fullmatch(prefix):
        raise ValueError(f"invalid prefix {prefix!r}")
    union = main_ids | claimed_ids
    max_num = 0
    for rid in union:
        if not re.fullmatch(_REQ_ID, rid):
            continue
        if re.search(r"-S\d+$", rid):
            continue
        m = _NUMBERED.match(rid)
        if not m or m.group("code") != prefix:
            continue
        max_num = max(max_num, int(m.group("num")))
    if max_num == 0:
        return f"{prefix}-01"
    return f"{prefix}-{max_num + 1:02d}"


def ids_from_unified_diff(diff: str) -> set[str]:
    """Ids from added requirement bullets in hunks touching ``.planning/REQUIREMENTS.md``."""
    claimed: set[str] = set()
    for chunk in diff.split("diff --git "):
        if not chunk.strip():
            continue
        header = chunk.split("\n", 1)[0]
        if not any(marker in header for marker in _REQ_MD_MARKERS):
            continue
        body = chunk.split("\n", 1)[1] if "\n" in chunk else ""
        for line in body.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            remainder = line[1:]
            claimed |= ids_from_requirements_text(remainder)
    return claimed


def parse_open_prs_json(text: str) -> set[str]:
    """Parse ``--open-prs-json`` fixture: explicit ids and/or PR diffs."""
    data = json.loads(text)
    if isinstance(data, list):
        if not data or all(isinstance(x, str) for x in data):
            return set(data) if data else set()
        raise ValueError("open-prs JSON list must contain only strings")
    if not isinstance(data, dict):
        raise TypeError("open-prs JSON must be an object, list of ids, or {\"ids\": ...}")
    if "ids" in data:
        raw = data["ids"]
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise ValueError('"ids" must be a list of strings')
        return set(raw)
    if "prs" in data:
        prs = data["prs"]
        if not isinstance(prs, list):
            raise ValueError('"prs" must be a list')
        claimed: set[str] = set()
        for item in prs:
            if not isinstance(item, dict):
                raise TypeError("each pr entry must be an object with a diff")
            diff = item.get("diff", "")
            if not isinstance(diff, str):
                raise TypeError("each pr diff must be a string")
            claimed |= ids_from_unified_diff(diff)
        return claimed
    raise ValueError('open-prs JSON needs "ids" or "prs"')


def _git_show_main(git: str, main_ref: str) -> tuple[str | None, str]:
    proc = subprocess.run(
        [git, "show", f"{main_ref}:{SOURCE}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode == 0:
        return proc.stdout, ""
    if "does not exist" in proc.stderr or "exists on disk, but not in" in proc.stderr:
        return "", ""
    return None, proc.stderr.strip() or f"git show exited {proc.returncode}"


# ----- live claims: one gh call, diffs from git ------------------------------------------
#: Hard cap on gh invocations per run. One ``gh pr list`` names the open PRs and their heads;
#: every diff is read from git (``refs/pull/<N>/head``), which spends no REST or GraphQL budget.
#: A ``gh pr diff`` per PR made 370 calls in 15 min on the human login (Wed 11 Nov 2026).
MAX_GH_CALLS = 1
OPEN_PR_LIMIT = 1000
_FETCH_CHUNK = 100
_CLAIM_PATHS = (SOURCE, f"{FRAGMENTS_REL}/")


class GhCallCapExceeded(RuntimeError):
    """A run tried more gh calls than ``MAX_GH_CALLS``."""


class GhCallBudget:
    """Counts gh invocations and refuses the one past ``cap``."""

    def __init__(self, cap: int = MAX_GH_CALLS) -> None:
        self.cap = cap
        self.spent = 0

    def spend(self, what: str) -> None:
        if self.spent >= self.cap:
            raise GhCallCapExceeded(f"gh call cap {self.cap} reached; refused: gh {what}")
        self.spent += 1


def _open_pr_heads(gh: str, repo: str, budget: GhCallBudget) -> tuple[dict[int, str] | None, str]:
    """``{number: headRefOid}`` for every open PR, from one ``gh pr list``."""
    budget.spend("pr list")
    proc = subprocess.run(
        [gh, "pr", "list", "--repo", repo, "--state", "open", "--limit", str(OPEN_PR_LIMIT),
         "--json", "number,headRefOid"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if proc.returncode != 0:
        return None, proc.stderr.strip() or f"gh pr list exited {proc.returncode}"
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, f"gh pr list returned invalid JSON: {exc}"
    if not isinstance(rows, list):
        return None, "gh pr list did not return a JSON list"
    if len(rows) >= OPEN_PR_LIMIT:
        return None, f"gh pr list returned {len(rows)} rows, the --limit; the open PR list may be truncated"
    heads: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("number"), int) or not row.get("headRefOid"):
            return None, f"gh pr list row missing number or headRefOid: {row!r}"
        heads[row["number"]] = str(row["headRefOid"])
    return heads, ""


def _has_commit(git: str, sha: str) -> bool:
    proc = subprocess.run([git, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, check=False)
    return proc.returncode == 0


def _fetch_pr_heads(git: str, numbers: list[int]) -> str:
    """Fetch ``refs/pull/<N>/head`` objects (git protocol, no API budget); "" or git's stderr."""
    for i in range(0, len(numbers), _FETCH_CHUNK):
        refspecs = [f"refs/pull/{n}/head" for n in numbers[i:i + _FETCH_CHUNK]]
        proc = subprocess.run(
            [git, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "origin", *refspecs],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if proc.returncode != 0:
            return proc.stderr.strip() or f"git fetch exited {proc.returncode}"
    return ""


def _claims_from_git(git: str, main_ref: str, heads: dict[int, str]) -> tuple[set[str] | None, str]:
    """Ids on ``+`` lines of ``main_ref..head`` over the requirement files, per open PR head.

    A two-dot tree diff (no merge base) is a superset of the PR's own additions: it may also
    show lines ``main_ref`` has since dropped, which can only over-claim (a skipped number),
    never under-claim. The merge-base form cost about a minute per run over ~300 open PRs.
    """
    missing = sorted(n for n, sha in heads.items() if not _has_commit(git, sha))
    if missing:
        err = _fetch_pr_heads(git, missing)
        if err:
            return None, f"git fetch refs/pull/*/head failed: {err}"
    claimed: set[str] = set()
    for number, sha in sorted(heads.items()):
        if not _has_commit(git, sha):
            return None, f"PR #{number} head {sha} not found after fetching refs/pull/{number}/head (re-run)"
        proc = subprocess.run(
            [git, "diff", main_ref, sha, "--", *_CLAIM_PATHS],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if proc.returncode != 0:
            return None, f"git diff {main_ref} {sha} (PR #{number}) failed: {proc.stderr.strip()}"
        claimed |= ids_from_unified_diff(proc.stdout)
    return claimed, ""


def _live_claimed_ids(repo: str, git: str, main_ref: str) -> tuple[set[str] | None, str]:
    gh = shutil.which("gh")
    if gh is None:
        return None, "gh is not on PATH"
    heads, err = _open_pr_heads(gh, repo, GhCallBudget())
    if heads is None:
        return None, err
    return _claims_from_git(git, main_ref, heads)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prefix", help="requirement code prefix (e.g. OPS)")
    parser.add_argument(
        "--open-prs-json",
        type=Path,
        default=None,
        help="fixture JSON for claimed ids (tests); skips gh",
    )
    parser.add_argument(
        "--main-text",
        type=Path,
        default=None,
        help="REQUIREMENTS.md text instead of git show (tests)",
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name for gh pr list")
    args = parser.parse_args(argv)

    prefix = args.prefix.strip()
    if not prefix:
        print("[UNKNOWN] prefix must not be empty", file=sys.stderr)
        return 2
    try:
        next_requirement_id(prefix, set(), set())
    except ValueError:
        print(f"[UNKNOWN] invalid prefix {prefix!r}", file=sys.stderr)
        return 2

    git = shutil.which("git")
    needs_git = args.main_text is None or args.open_prs_json is None
    if needs_git and git is None:
        print("[UNKNOWN] git is not on PATH", file=sys.stderr)
        return 2
    if args.main_text is not None:
        main_text = args.main_text.read_text(encoding="utf-8")
    else:
        assert git is not None
        main_text, err = _git_show_main(git, _MAIN_REF)
        if main_text is None:
            print(f"[UNKNOWN] cannot read {_MAIN_REF}:{SOURCE}: {err}", file=sys.stderr)
            return 2
        fragments, err = fragments_text_at(git, _MAIN_REF)
        if fragments is None:
            print(f"[UNKNOWN] cannot read {_MAIN_REF}:{FRAGMENTS_REL}/: {err}", file=sys.stderr)
            return 2
        main_text += "\n" + fragments

    main_ids = ids_from_requirements_text(main_text)

    if args.open_prs_json is not None:
        try:
            claimed_ids = parse_open_prs_json(args.open_prs_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError, OSError) as exc:
            print(f"[UNKNOWN] --open-prs-json: {exc}", file=sys.stderr)
            return 2
    else:
        assert git is not None
        claimed_ids, err = _live_claimed_ids(args.repo, git, _MAIN_REF)
        if claimed_ids is None:
            print(f"[UNKNOWN] {err}", file=sys.stderr)
            return 2

    result = next_requirement_id(prefix, main_ids, claimed_ids)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
