"""Reading `.planning/TECH-DEBT.md` at a pinned commit, and what counts as read.

Split out of `scripts/review_thread_triage.py` at the 600-line ceiling, on the
seam the module already had: everything here answers "what does the ledger say
at this revision", while the caller decides what to do about the answer. The
seam is load-bearing rather than cosmetic - `LedgerReadError` exists to keep a
FAILED MEASUREMENT from rendering as a negative verdict, and that distinction
belongs with the reading, not with the rendering.

Nothing here is mocked: `_ledger_at` shells out to the real `gh api` and a
failure is raised, never defaulted to an empty set.
"""

from __future__ import annotations

import json
import re
import subprocess

OWNER = "maintainer"
REPO = "music-dj-tools"

_LEDGER_PATH = ".planning/TECH-DEBT.md"
_DEBT_PATH = ".planning/debt/{number}.md"

# The ledger is the other half of a DEBT-LOGGED claim. A reply naming an anchor
# that was never appended loses the finding just as silently as saying nothing.
_PERMALINK = re.compile(r"https://github\.com/\S+?#discussion_r\d+")


class LedgerReadError(RuntimeError):
    """The ledger could not be read at the PR head. Never rendered as a verdict.

    #792: falling back to a local checkout's `.planning/TECH-DEBT.md`, or to an
    empty permalink set on any failure, both turn a broken measurement into an
    accusation -- the gate asserting a real, branch-local debt entry is
    missing. Neither shortcut is available here; a failed read must surface as
    unmeasured, exactly like `review_coverage.TriageError`.
    """


class LedgerFileNotFoundError(LedgerReadError):
    """The ref resolved, but `.planning/TECH-DEBT.md` does not exist there.

    #805 round 7 (review_ledger.py:97): a commit predating the ledger's own
    creation -- an old PR head, or a feature branch a stacked PR's base has
    not rebased since -- is a LEGITIMATE empty read, not a failed one. Raising
    the same undifferentiated `LedgerReadError` here made `_ledger_permalinks`
    abort the whole union the instant any one of several sources happened to
    predate the file, even when a canonical match sat in a different, readable
    source. A subclass of `LedgerReadError` so a caller asking for `_ledger_at`
    at one specific ref (never a union) still sees a raise either way -- only
    `_ledger_permalinks`'s union loop distinguishes the two.
    """


def debt_permalinks_from_text(text: str) -> frozenset[str]:
    """Every review-thread URL explicitly named by one PR debt file."""
    return frozenset(_PERMALINK.findall(text))


def debt_text_at(number: int, head_sha: str, owner: str = OWNER, repo: str = REPO) -> str:
    """Read one PR's debt file at its immutable reviewed head.

    A missing file is a measured empty ledger. Any other failure raises, so a
    caller can fail open instead of treating an unread ledger as a match.
    """
    path = _DEBT_PATH.format(number=number)
    result = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{owner}/{repo}/contents/{path}?ref={head_sha}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    if _is_missing_file(result.stdout) and _path_exists_at(head_sha, path, owner, repo) is False:
        return ""
    raise LedgerReadError(
        f"could not read {path} at {head_sha}: {result.stderr.strip() or '<no stderr>'}"
    )


def _debt_permalinks(
    number: int, head_sha: str, owner: str = OWNER, repo: str = REPO
) -> frozenset[str]:
    """Read only PR `number`'s debt file from its immutable branch head.

    The runner checkout is deliberately irrelevant. A missing per-PR file is
    a measured empty set: it makes a DEBT-LOGGED thread fail, while a failed
    remote read still raises rather than being mistaken for absence.
    """
    path = _DEBT_PATH.format(number=number)
    result = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{owner}/{repo}/contents/{path}?ref={head_sha}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return debt_permalinks_from_text(result.stdout)
    if _is_missing_file(result.stdout):
        missing_path = _path_exists_at(head_sha, path, owner=owner, repo=repo) is False
        if missing_path:
            return frozenset()
    raise LedgerReadError(
        f"could not read {path} at {head_sha}: {result.stderr.strip() or '<no stderr>'}"
    )


def _is_missing_file(stdout: str) -> bool:
    """True for the Contents API's generic "Not Found" 404 body -- the shape
    used both for "this path is absent at a ref that DID resolve" AND, per
    GitHub's REST troubleshooting docs, for a token that can read the ref but
    lacks Contents permission on it: the API deliberately returns the same
    message rather than confirm the resource exists to an unauthorized
    caller. This function alone can no longer tell the two apart -- see
    `_path_exists_at`, which `_ledger_at` calls for independent evidence
    before narrowing to `LedgerFileNotFoundError`. #805 round 10 (Codex,
    discussion_r3918067590, review_ledger.py:68).

    A deleted branch or a bad SHA is also a 404, but its body reads "No commit
    found for the ref ..." -- a genuine read failure, not an empty ledger, and
    conflating the two would turn a real outage into a silent empty union.
    Checked on the parsed JSON body's `message`, the one field the Contents
    API itself uses to distinguish those two 404s, not on the HTTP status alone.
    """
    try:
        body = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(body, dict) and body.get("message") == "Not Found"


def _path_exists_at(ref: str, path: str, owner: str = OWNER, repo: str = REPO) -> bool | None:
    """Independent evidence for whether `path` exists at `ref`, via the git
    Trees API's recursive listing rather than the Contents API's message-
    sniffed 404 that `_is_missing_file` alone can no longer trust.

    Returns `None`, never a guess, whenever the control itself cannot settle
    the question: the tree read failed outright (the same Contents-scope
    denial that masks as a 404 on the direct file read would mask here too,
    since both endpoints sit behind the same permission), or GitHub truncated
    the listing (`truncated: true` past its size cap, real at 6233 entries as
    of #805 round 10 but not a bound this function may assume stays true) --
    see `.claude/rules/verification.md`'s "a truncated listing whose absent
    entry is read as a thing that does not exist".

    A `True` result -- the tree lists `path` even though the direct Contents
    read 404'd -- is the load-bearing branch, not defensive padding: that
    combination IS the masked-permission signature this function exists to
    catch, and `_ledger_at` must not narrow to `LedgerFileNotFoundError` on it.
    """
    result = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/git/trees/{ref}?recursive=1"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        body = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("truncated") or "tree" not in body:
        return None
    return path in {entry.get("path") for entry in body["tree"]}


def _ledger_at(ref: str, owner: str = OWNER, repo: str = REPO) -> frozenset[str]:
    """Every review-thread permalink in `.planning/TECH-DEBT.md` at one ref."""
    result = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{owner}/{repo}/contents/{_LEDGER_PATH}?ref={ref}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if _is_missing_file(result.stdout):
            if _path_exists_at(ref, _LEDGER_PATH, owner=owner, repo=repo) is False:
                raise LedgerFileNotFoundError(
                    f"{_LEDGER_PATH} does not exist at {ref} (this commit "
                    f"predates the ledger's own creation): {result.stderr.strip()}"
                )
            raise LedgerReadError(
                f"could not confirm {_LEDGER_PATH} is absent at {ref}: the Contents "
                "API's 404 may mask an authorization failure rather than a genuinely "
                "missing file (GitHub's REST troubleshooting docs document this "
                "masking), and the independent tree listing could not settle it "
                f"either: {result.stderr.strip()}"
            )
        raise LedgerReadError(
            f"could not read {_LEDGER_PATH} at {ref}: "
            f"{result.stderr.strip() or '<no stderr>'}"
        )
    return frozenset(_PERMALINK.findall(result.stdout))


def _ledger_permalinks(
    head_sha: str,
    base_ref: str = "main",
    *extra_refs: str,
    owner: str = OWNER,
    repo: str = REPO,
) -> frozenset[str]:
    """The ledgers a DEBT-LOGGED claim may legitimately live in.

    A DEBT-LOGGED reply's claim usually lives in the PR's own commits, and
    #792 was about reading any OTHER tree - the runner's local checkout, a
    merge-lane worktree parked on unrelated work - and reporting the answer as
    though it were about this PR.

    But the head is not the only legitimate home. `.planning/TECH-DEBT.md`
    documents appends pushed DIRECTLY TO MAIN (docs-only, CI-free, the same
    convention `.planning/MERGE-QUEUE.md` uses), and a PR branch that has not
    rebased since such an append does not contain it. Reading the head alone
    therefore reported a real entry as DEBT-LOG NOT IN LEDGER and blocked the
    merge: the same accusation #792 fixed, arriving from the other direction.
    Codex found it on #805.

    So the answer is the UNION. A permalink counts as logged if it is in the
    ledger the PR ships or in the canonical one on its base branch.

    The base ref is MUTABLE, unlike the pinned head, and deliberately not
    guarded by `_require_stable_head`. A base that moves mid-run can only ADD
    entries, which can only turn a refusal into a pass; the failure this whole
    module is built against is the opposite direction, asserting an entry is
    missing when it is not.

    A source predating the ledger's own creation (`LedgerFileNotFoundError`)
    contributes nothing and does not stop the union -- it was READ, it is just
    empty there, so a match in any other source still wins (#805 round 7). Any
    OTHER read failure still raises and aborts the whole union: a half-read
    union is unmeasured, and an unmeasured ledger must never render as a
    verdict - see `LedgerReadError`.
    """
    refs = dict.fromkeys((head_sha, base_ref, *extra_refs))
    union: frozenset[str] = frozenset()
    for ref in refs:
        try:
            union |= _ledger_at(ref, owner=owner, repo=repo)
        except LedgerFileNotFoundError:
            continue
    return union
