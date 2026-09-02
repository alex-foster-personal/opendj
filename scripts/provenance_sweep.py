"""Unified prompt+provenance sweep across Claude Code, Codex and Cursor.

Harvests what the HUMAN typed in every agent conversation touching a repo, links
each prompt to the branch and PR that was live when it was sent, and maintains a
durable JSONL ledger plus a readable markdown index.

Designed to run from a Claude Code Stop hook (turn-end). Because it reads all
three stores straight off disk, ONE trigger sweeps all three tools - Cursor and
Codex need no hook of their own.

MERGE-PRESERVING BY CONSTRUCTION. The ledger is tracked repo content, so every
checkout holds its branch's snapshot of it, while the freshness watermark is
gitignored and per-checkout. Those two can disagree, and when they do a naive
incremental sweep writes a REGRESSED ledger that merges back over a fuller one.
Every run therefore unions its harvest with the ledger committed at HEAD and at
the trunk, and refuses to write an output that is not a superset of both.

Requirements (mini-PRD)
  1. Human prompts only, all three tools                        [OK] done
     - if a CC record has isSidechain/isMeta then EXCLUDE (subagent noise)
     - if a Codex record is event_msg/user_message then INCLUDE
     - if a Cursor bubble has type == 1 then INCLUDE (2 == assistant)
  2. Scoped to one repo, worktrees included                     [OK] done
     - if the session cwd does not match the repo's MAIN worktree then EXCLUDE
     - if the sweep runs from a linked worktree then it still sees every session
  3. Linked to branch and PR                                    [OK] done
     - if a prompt's timestamp falls in a branch's active window then attach it
     - if that branch has a PR then attach number, state and URL
  4. Cheap enough for a turn-end hook                           [OK] done
     - if a source is unchanged since the watermark then SKIP it entirely
     - if the ledger is behind the watermark's fingerprint then ignore the
       watermark and re-harvest in full, because the checkout is stale
  5. Never loses a row it has previously recorded               [OK] done
     - if the output would drop a row held at HEAD or at the trunk then ABORT
     - if --full is passed then re-harvest everything and still union, never
       truncate the ledger to whatever happens to be on disk today

Usage (run as a module: it imports its sibling modules by package path, so a
direct `./provenance_sweep.py` cannot resolve them and no shebang pretends it
can)
  uv run --no-project python -m scripts.provenance_cli --repo <path>

This module is the LIBRARY half: harvesting, the tracked ledger, and the write
guard. The command lives in :mod:`scripts.provenance_cli`, which owns argument
parsing, the refusal protocol and the entry point. Split there when this file
crossed 600 lines a second time, on the seam the tests were already split on.
  ... --repo <path> --full           # re-harvest everything, still unions
  ... --repo <path> --stats          # report, write nothing
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.provenance_index import (
    INDEX,
    INDEX_PATH,
    _comparable,
    _index_view,
    _without_swept_stamp,
    index_rows,
    render_index,
)
from scripts.provenance_links import TRUNK_BRANCH
from scripts.provenance_sources import (
    KEY_PREFIX,
    _git,
)
from scripts.provenance_state import (
    _write_atomic,
)

# -----------------------------------------------------------------------------
# CFG
# -----------------------------------------------------------------------------

OUT_DIRNAME = "docs/threads"
LEDGER = "prompts.jsonl"
WATERMARK = ".provenance-watermark.json"
LOCKFILE = ".provenance-lock"
LEDGER_PATH = f"{OUT_DIRNAME}/{LEDGER}"

# Fields that define WHICH prompt a row is. A re-harvest may refresh anything
# else on a known row, but never these.
IDENTITY_FIELDS = frozenset({"tool", "at", "text"})


# -----------------------------------------------------------------------------
# _helpers
# -----------------------------------------------------------------------------


def row_key(rec: dict) -> str:
    """Stable identity of one harvested prompt.

    Deliberately the same shape the ledger was originally built with: widening it
    would re-admit rows the existing file already deduplicated away, so the
    superset guard would fire on its own history.
    """
    return f"{rec.get('tool')}|{rec.get('at')}|{rec.get('text', '')[:KEY_PREFIX]}"


def tag_forks(prompts: list[dict]) -> None:
    """Mark replayed prompts as forks instead of letting them inflate the count.

    Resuming or forking a session copies the earlier turns into a NEW jsonl
    VERBATIM, timestamp included, so the same typed sentence appears under
    several ssids at the SAME instant. The first occurrence is the original; the
    rest are `fork: true` and carry `fork_of` so the lineage stays visible.

    The timestamp is what makes this a lineage test rather than a text test.
    Keyed on text alone, a human retyping a short request - "check then merge" is
    in this ledger four times - had every later occurrence marked a fork, and
    `render_index` drops fork rows from both the table and the prompt count, so
    genuine prompts vanished from the readable record. A replay carries the
    original instant; a retype cannot.

    Run over the WHOLE union, never over one incremental batch: a batch-local
    answer flips as sessions are re-touched, so the same row would toggle between
    original and fork run to run.

    KNOWN DEAD, and said here rather than left to be rediscovered: with `at` in
    the key this function can no longer flag anything, because `write_out`
    deduplicates on `row_key` (tool|at|text[:80]) BEFORE it runs, and that is
    the same shape. A true replay is collapsed into one row upstream and never
    reaches the loop below. Measured on the 1,660-row ledger: 0 surviving
    row_key collisions, 556 flags under the old text-only key, 0 under this one.
    Those 556 were not replays - 173 were a cron voice prompt under 173 distinct
    ssids at 173 distinct instants, spanning a month - so clearing them is the
    point, and `render_index` stops hiding them. Making genuine replays
    detectable needs replay-distinguishing identity to survive deduplication,
    which changes ledger identity: .planning/TECH-DEBT.md#pr-708.
    """
    for p in prompts:
        p.pop("fork", None)
        p.pop("fork_of", None)
    first: dict[str, dict] = {}
    for p in sorted(prompts, key=lambda r: (r.get("at") or "", r.get("ssid", ""))):
        # `at` in the key is the lineage evidence: same text at the same instant
        # under a different ssid is a replayed turn, not a second typing.
        key = f"{p['tool']}|{p.get('at')}|{p['text'][:160]}"
        origin = first.get(key)
        if origin is None:
            first[key] = p
        else:
            p["fork"] = True
            p["fork_of"] = origin.get("ssid")


# -----------------------------------------------------------------------------
# sources
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# linking
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# prior committed state (the anti-regression memory)
# -----------------------------------------------------------------------------


def parse_ledger(text: str) -> list[dict]:
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


_BLOBS: dict[tuple[str, str], str | None] = {}


def guard_shas(repo: Path) -> list[str]:
    """Commit ids of the refs whose ledger the output must not fall behind.

    Deduplicated, because HEAD usually IS the trunk and reading the same tree
    twice doubles the git subprocess cost of every sweep. Resolved fresh on each
    call rather than cached by ref NAME: a test (and a real branch) moves HEAD
    mid-run, and a name-keyed cache would then answer for the wrong commit.
    """
    shas: list[str] = []
    head = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").strip()
    if head:
        shas.append(head)
    listed = _git(
        repo, "for-each-ref", "--format=%(objectname)",
        f"refs/heads/{TRUNK_BRANCH}", f"refs/remotes/origin/{TRUNK_BRANCH}",
    ).split()
    for sha in listed:
        if sha not in shas:
            shas.append(sha)
    return shas


def _blob(repo: Path, sha: str, path: str) -> str | None:
    """Read one tracked file at one commit, or None if the path is absent there.

    Cached by (sha, path): a commit's tree is immutable, so re-reading it inside
    one run can only return what it already returned.

    This is a subprocess argv, never a shell string, so the zsh `${SHA}:path`
    modifier trap that governs the interactive form does not apply here.
    """
    key = (sha, path)
    if key in _BLOBS:
        return _BLOBS[key]
    p = subprocess.run(
        ("git", "-C", str(repo), "show", f"{sha}:{path}"),
        capture_output=True, text=True, check=False,
    )
    _BLOBS[key] = p.stdout if p.returncode == 0 else None
    return _BLOBS[key]


def committed_rows(repo: Path) -> dict[str, dict]:
    """Every prompt row recorded in the ledger at HEAD or at the trunk.

    This is the memory that makes the sweep merge-preserving. The gitignored
    watermark cannot supply it: a checkout can hold a fresh watermark and a stale
    ledger at the same time, which is exactly the state that produced the loss
    this function exists to prevent.
    """
    found: dict[str, dict] = {}
    for sha in guard_shas(repo):
        text = _blob(repo, sha, LEDGER_PATH)
        if text is None:
            continue
        for rec in parse_ledger(text):
            found.setdefault(row_key(rec), rec)
    return found


# -----------------------------------------------------------------------------
# output
# -----------------------------------------------------------------------------


class RegressionError(RuntimeError):
    """The sweep would have dropped a row it had already recorded."""


def assert_superset(allrecs: list[dict], repo: Path, prior: dict[str, dict]) -> None:
    """Refuse to write output that loses a row held at HEAD or at the trunk.

    Checks the PRESENCE of every previously recorded row, not the absence of a
    diff: a sweep that harvested nothing and a sweep that harvested everything
    both produce an empty diff against themselves, and only one of them is
    correct.
    """
    have = {row_key(r) for r in allrecs}
    missing = [rec for key, rec in prior.items() if key not in have]
    if missing:
        sample = "\n".join(
            f"    {r.get('at')}  {r.get('tool')}  {r.get('text', '')[:70]!r}"
            for r in missing[:5]
        )
        raise RegressionError(
            f"output would drop {len(missing)} row(s) already committed to "
            f"{LEDGER_PATH}; refusing to write.\n{sample}"
        )

    view = {_comparable(_index_view(r)) for r in allrecs}
    for sha in guard_shas(repo):
        text = _blob(repo, sha, INDEX_PATH)
        if text is None:
            continue
        gone = {_comparable(r) for r in index_rows(text)} - view
        if gone:
            sample = "\n".join(f"    {w}  {t}  {x[:70]!r}" for w, t, x in list(gone)[:5])
            raise RegressionError(
                f"output would drop {len(gone)} row(s) present in the index at "
                f"{sha[:8]}; refusing to write.\n{sample}"
            )


def write_out(repo: Path, prompts: list[dict], prior: dict[str, dict]) -> tuple[int, int]:
    """Union harvest + on-disk ledger + committed ledgers, then write both files.

    The ledger is rewritten from the union rather than appended to, so the file
    is a pure function of the union and a re-run cannot reorder or duplicate it.
    """
    out_dir = repo / OUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = out_dir / LEDGER

    merged: dict[str, dict] = dict(prior)
    if ledger.exists():
        for rec in parse_ledger(ledger.read_text(errors="ignore")):
            merged.setdefault(row_key(rec), rec)
    fresh = 0
    for rec in prompts:
        key = row_key(rec)
        if key not in merged:
            merged[key] = rec
            fresh += 1
        else:
            # A re-harvest of a known row carries fresher branch/PR links, but
            # NEVER its identity fields. row_key spans the first 80 characters
            # while the index renders 96, so overwriting `text` lets a row whose
            # prefix collides silently rewrite the rendered cell, and the guard
            # then reads its own churn as data loss (hit on the real ledger).
            merged[key].update({
                k: v for k, v in rec.items()
                if v is not None and k not in IDENTITY_FIELDS
            })

    allrecs = list(merged.values())
    tag_forks(allrecs)
    allrecs.sort(key=lambda r: (r.get("at") or "", r.get("ssid", "")), reverse=True)

    assert_superset(allrecs, repo, prior)

    body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in allrecs)
    if not ledger.exists() or ledger.read_text(errors="ignore") != body:
        _write_atomic(ledger, body)

    content = render_index(allrecs)
    index_file = out_dir / INDEX
    # A no-change sweep must not touch the tracked index: bumping only the
    # volatile "Swept ..." stamp dirties whichever checkout the Stop hook ran
    # in, so every worktree thread ended on an uncommitted-changes warning
    # (hit repeatedly through Mon 31 Aug 2026). Freshness already lives in the
    # gitignored watermark file.
    if (not index_file.exists()
            or _without_swept_stamp(index_file.read_text()) != _without_swept_stamp(content)):
        _write_atomic(index_file, content)
    return fresh, len(allrecs)


# -----------------------------------------------------------------------------
# watermark
# -----------------------------------------------------------------------------
