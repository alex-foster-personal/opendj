"""Unified prompt+provenance sweep across Claude Code, Codex and Cursor.

Harvests what the HUMAN typed in every agent conversation touching a repo, links
each prompt to the branch and PR that was live when it was sent, and maintains a
durable JSONL ledger plus a readable markdown index.

Designed to run from a Claude Code Stop hook (turn-end). Because it reads all
three stores straight off disk, ONE trigger sweeps all three tools - Cursor and
Codex need no hook of their own.

OFF THE TRACKED TREE BY CONSTRUCTION (the provenance-ledger-off-tree ADR). The
ledger, its index, the watermark and the lock all live in one STORE directory
that the caller names with `--out-dir` and that the CLI refuses unless it is
outside every tracked working tree. The ledger used to be tracked repo content,
so every turn-end sweep dirtied the main checkout of every machine running a
session here, and autoreposync skipped that checkout as dirty until FleetDash
went red (Air, Sun 18 Oct 2026). A hook that writes the tracked tree is the
defect; publishing the ledger is a separate, explicit step
(:mod:`scripts.provenance_publish`).

Every run still unions its harvest with the ledger already in the store and
refuses to write an output that is not a superset of it.

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
       watermark and re-harvest in full, because the store moved under it
  5. Never loses a row it has previously recorded               [OK] done
     - if the output would drop a row already in the store then ABORT
     - if --full is passed then re-harvest everything and still union, never
       truncate the ledger to whatever happens to be on disk today
  6. Never writes a tracked working tree                        [OK] done
     - if --out-dir resolves inside a git work tree and is not ignored there
       then REFUSE before reading any source
     - if a session ends in a checkout then `git status --porcelain` is
       unchanged by the sweep

Usage (run as a module: it imports its sibling modules by package path, so a
direct `./provenance_sweep.py` cannot resolve them and no shebang pretends it
can)
  uv run --no-project python -m scripts.provenance_cli --repo <path> --out-dir <store>

This module is the LIBRARY half: the ledger store and the write guard. The command lives in :mod:`scripts.provenance_cli`, which owns argument
parsing, the refusal protocol and the entry point. Split there when this file
crossed 600 lines a second time, on the seam the tests were already split on.
  ... --repo <path> --full           # re-harvest everything, still unions
  ... --repo <path> --stats          # report, write nothing
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.provenance_index import (
    INDEX,
    _comparable,
    _index_view,
    _without_swept_stamp,
    index_rows,
    render_index,
)
from scripts.provenance_sources import KEY_PREFIX
from scripts.provenance_state import (
    _write_atomic,
    make_store,
)

# -----------------------------------------------------------------------------
# CFG
# -----------------------------------------------------------------------------

LEDGER = "prompts.jsonl"

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
    which changes ledger identity: (private path)#pr-708.
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
# prior state (the anti-regression memory)
# -----------------------------------------------------------------------------


class RegressionError(RuntimeError):
    """The sweep would have dropped a row it had already recorded."""


class LedgerUnreadable(RegressionError):
    """A ledger holds bytes or a line that cannot be read as a row.

    A RegressionError because that is what reading past it would cause: the
    damaged ledger, read as a shorter one, would be replaced without the rows
    it lost.
    """


def _row_defect(rec: dict) -> str | None:
    """Why one parsed row cannot be a ledger row, or None.

    Every field a later step consumes, checked where the row is READ, so a
    damaged row is a recorded refusal instead of a KeyError or TypeError deep
    in `tag_forks`, the sort, or `render_index` after a file was already
    replaced (Codex and Sol, #7403). Shapes are the ones all 2,800 published
    rows have: `at` may be null (171 rows), `ssid` is always a non-empty
    string, `prs` and `branches` are optional lists.
    """
    if not (isinstance(rec.get("tool"), str) and isinstance(rec.get("text"), str)):
        return "lacks a string tool or text"
    if not isinstance(rec.get("at"), (str, type(None))):
        return "has an at that is neither a string nor null"
    ssid = rec.get("ssid")
    if not isinstance(ssid, str) or not ssid:
        return "lacks a non-empty string ssid"
    prs = rec.get("prs")
    if prs is not None and not (
        isinstance(prs, list)
        and all(isinstance(pr, dict) and isinstance(pr.get("number"), int)
                and isinstance(pr.get("url"), str) for pr in prs)
    ):
        return "has prs that are not a list of {number: int, url: str}"
    branches = rec.get("branches")
    if branches is not None and not (
        isinstance(branches, list) and all(isinstance(b, str) for b in branches)
    ):
        return "has branches that are not a list of strings"
    return None


def parse_ledger(text: str, *, origin: str = "ledger") -> list[dict]:
    """Every row of a ledger, refusing any line that is not one JSON object.

    Strict on purpose. Skipping an unparseable line reads a damaged ledger as
    a SHORTER one, and the next write then replaces it without that row: the
    superset guard cannot object to a row it was never shown (Sol, #7403).
    """
    out: list[dict] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerUnreadable(f"{origin}:{number} is not a JSON row: {exc}") from exc
        if not isinstance(rec, dict):
            raise LedgerUnreadable(f"{origin}:{number} is not a JSON object")
        defect = _row_defect(rec)
        if defect is not None:
            raise LedgerUnreadable(f"{origin}:{number} {defect}")
        out.append(rec)
    return out


def read_ledger_file(path: Path) -> dict[str, dict]:
    """Every row of one ledger file, keyed by `row_key`; empty when it is absent.

    Decoded strictly: `errors="ignore"` dropped invalid bytes and could hand
    back an altered row as if it were the stored one (Sol, #7403).
    """
    found: dict[str, dict] = {}
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise LedgerUnreadable(f"{path} is not valid UTF-8: {exc}") from exc
        except OSError as exc:
            # Unreadable is unmeasured, not empty: a refusal, never a traceback
            # into the Stop hook's discarded streams (Codex, #7403).
            raise LedgerUnreadable(f"{path} cannot be read: {exc}") from exc
        for rec in parse_ledger(text, origin=str(path)):
            found.setdefault(row_key(rec), rec)
    return found


def stored_rows(store: Path) -> dict[str, dict]:
    """Every prompt row the store already holds: what the output must keep."""
    return read_ledger_file(store / LEDGER)


# -----------------------------------------------------------------------------
# output
# -----------------------------------------------------------------------------


def assert_superset(allrecs: list[dict], store: Path, prior: dict[str, dict]) -> None:
    """Refuse to write output that loses a row already in the store.

    Checks the PRESENCE of every previously recorded row, not the absence of a
    diff: a sweep that harvested nothing and a sweep that harvested everything
    both produce an empty diff against themselves, and only one of them is
    correct. The index on disk is checked the same way, in its lossy view.
    """
    have = {row_key(r) for r in allrecs}
    missing = [rec for key, rec in prior.items() if key not in have]
    if missing:
        sample = "\n".join(
            f"    {r.get('at')}  {r.get('tool')}  {r.get('text', '')[:70]!r}"
            for r in missing[:5]
        )
        raise RegressionError(
            f"output would drop {len(missing)} row(s) already in "
            f"{store / LEDGER}; refusing to write.\n{sample}"
        )

    index_file = store / INDEX
    if not index_file.exists():
        return
    try:
        index_text = index_file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        # Same rule as the ledger: unmeasured is a recorded refusal, not a
        # traceback into the hook's discarded streams (Codex and Grok, #7403).
        raise LedgerUnreadable(f"{index_file} cannot be read: {exc}") from exc
    view = {_comparable(_index_view(r)) for r in allrecs}
    gone = {_comparable(r) for r in index_rows(index_text)} - view
    if gone:
        sample = "\n".join(f"    {w}  {t}  {x[:70]!r}" for w, t, x in list(gone)[:5])
        raise RegressionError(
            f"output would drop {len(gone)} row(s) present in {index_file}; "
            f"refusing to write.\n{sample}"
        )


def write_out(store: Path, prompts: list[dict], prior: dict[str, dict]) -> tuple[int, int]:
    """Union harvest + the store's ledger + `prior`, then write ledger and index.

    The ledger is rewritten from the union rather than appended to, so the file
    is a pure function of the union and a re-run cannot reorder or duplicate it.
    """
    make_store(store)
    ledger = store / LEDGER

    merged: dict[str, dict] = dict(prior)
    for key, rec in read_ledger_file(ledger).items():
        merged.setdefault(key, rec)
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

    assert_superset(allrecs, store, prior)

    # Both outputs are produced BEFORE either file is replaced, so a render
    # failure can never leave a new ledger beside an old index (Codex, #7403).
    body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in allrecs)
    content = render_index(allrecs)
    if not ledger.exists() or ledger.read_text(encoding="utf-8") != body:
        _write_atomic(ledger, body)

    index_file = store / INDEX
    # A no-change sweep does not rewrite the index for its volatile "Swept ..."
    # stamp alone. When the index lived in the tracked tree that stamp dirtied
    # every checkout (Mon 31 Aug 2026); in the store it would still make every
    # publish see a change that carries no new row.
    if (not index_file.exists()
            or _without_swept_stamp(index_file.read_text(encoding="utf-8")) != _without_swept_stamp(content)):
        _write_atomic(index_file, content)
    return fresh, len(allrecs)


# -----------------------------------------------------------------------------
# watermark
# -----------------------------------------------------------------------------
