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
  uv run --no-project python -m scripts.provenance_sweep --repo <path>
  ... --repo <path> --full           # re-harvest everything, still unions
  ... --repo <path> --stats          # report, write nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.provenance_links import TRUNK_BRANCH, attach_links, branch_windows, pr_map
from scripts.provenance_sources import _git, harvest_claude, harvest_codex, harvest_cursor

# -----------------------------------------------------------------------------
# CFG
# -----------------------------------------------------------------------------

OUT_DIRNAME = "docs/threads"
LEDGER = "prompts.jsonl"
INDEX = "PROMPT-INDEX.md"
WATERMARK = ".provenance-watermark.json"
LEDGER_PATH = f"{OUT_DIRNAME}/{LEDGER}"
INDEX_PATH = f"{OUT_DIRNAME}/{INDEX}"


# How many rows the markdown table renders. The ledger is the complete store; the
# index is a newest-first WINDOW over it, and says so in its own header so the
# window is never mistaken for the whole.
INDEX_WINDOW = 400

# How many characters of a prompt identify it. The ledger was built at 80, so
# widening it would re-admit rows the existing file already deduplicated away.
KEY_PREFIX = 80

# Fields that define WHICH prompt a row is. A re-harvest may refresh anything
# else on a known row, but never these.
IDENTITY_FIELDS = frozenset({"tool", "at", "text"})


# Markdown cell boundary: a pipe that is not backslash-escaped.
CELL_SPLIT = re.compile(r"(?<!\\)\|")


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

    Resuming or forking a session copies the earlier turns into a NEW jsonl, so
    the same typed sentence legitimately appears under several ssids. The first
    occurrence by timestamp is the original; the rest are `fork: true` and carry
    `fork_of` so the lineage stays visible.

    Run over the WHOLE union, never over one incremental batch: a batch-local
    answer flips as sessions are re-touched, so the same row would toggle between
    original and fork run to run.
    """
    for p in prompts:
        p.pop("fork", None)
        p.pop("fork_of", None)
    first: dict[str, dict] = {}
    for p in sorted(prompts, key=lambda r: (r.get("at") or "", r.get("ssid", ""))):
        key = f"{p['tool']}|{p['text'][:160]}"
        origin = first.get(key)
        if origin is None:
            first[key] = p
        elif origin.get("ssid") != p.get("ssid"):
            p["fork"] = True
            p["fork_of"] = origin.get("ssid")
        else:
            p["fork"] = True  # same session replayed the line
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


def index_rows(text: str) -> set[tuple[str, str, str]]:
    """(when, tool, truncated-text) for each data row of a rendered index table.

    The index stores truncated fields, so a row cannot be turned back into a
    ledger key. Comparison therefore happens in this lossy view, which both
    sides can be projected into.
    """
    out: set[tuple[str, str, str]] = set()
    for line in text.splitlines():
        if not line.startswith("| ") or line.startswith("| when "):
            continue
        # Split on UNESCAPED pipes only. A prompt containing "|" is rendered as
        # "\|", and a naive split truncates that cell, so the guard would report
        # the row missing from its own output and abort every sweep forever.
        cells = [c.strip() for c in CELL_SPLIT.split(line.strip().strip("|"))]
        if len(cells) < 3:
            continue
        out.add((cells[0], cells[1], cells[2]))
    return out


def _comparable(row: tuple[str, str, str]) -> tuple[str, str, str]:
    """Narrow a rendered index row to the granularity the ledger can answer at.

    `row_key` identifies a prompt by its first 80 characters; the table renders
    96. Comparing at 96 asks a finer question than the store keeps an answer to,
    so two rows the ledger considers the same can render differently and read as
    a loss. Truncating both sides to KEY_PREFIX asks the answerable question.
    """
    when, tool, text = row
    return (when, tool, text[:KEY_PREFIX].strip())


def _index_view(rec: dict) -> tuple[str, str, str]:
    when = (rec.get("at") or "")[:16].replace("T", " ") or "-"
    # .strip() AFTER truncation, not before: a 96-char cut lands on a space often
    # enough, and a markdown cell reader strips it back off. Without this the
    # rendered row no longer matches its own source row, and the superset guard
    # reports 46 present rows as lost (measured against the real index).
    text = rec.get("text", "").replace("|", "\\|").replace("\n", " ")[:96].strip()
    return (when, rec.get("tool", "?"), text)


# -----------------------------------------------------------------------------
# output
# -----------------------------------------------------------------------------


def _without_swept_stamp(text: str) -> list[str]:
    """Index content minus the volatile 'Swept ...' line, for change detection."""
    return [ln for ln in text.splitlines() if not ln.startswith("Swept ")]


def render_index(allrecs: list[dict]) -> str:
    originals = [r for r in allrecs if not r.get("fork")]
    forks = len(allrecs) - len(originals)
    by_tool: dict[str, int] = {}
    for r in originals:
        by_tool[r.get("tool", "?")] = by_tool.get(r.get("tool", "?"), 0) + 1

    shown = originals[:INDEX_WINDOW]
    window_note = (
        f"Showing the newest {len(shown)} of {len(originals)}. "
        f"The complete record is `{LEDGER}`; this table is a window over it."
    )
    lines = [
        "# Prompt index (all agents)",
        "",
        "Every human prompt across Claude Code, Codex and Cursor for this repo,",
        "newest first, linked to the branch and PR live at the time.",
        "",
        f"Swept {datetime.now().astimezone().strftime('%a %d %b %Y %H:%M')}. ",
        f"{len(originals)} prompts typed: "
        + ", ".join(f"{k} {v}" for k, v in sorted(by_tool.items()))
        + (f"  (+{forks} fork replays, excluded)" if forks else ""),
        "",
        window_note,
        "",
        "| when | tool | prompt | branch / PR |",
        "|---|---|---|---|",
    ]
    for r in shown:
        when, tool, text = _index_view(r)
        link = ""
        if r.get("prs"):
            link = " ".join(f"[#{p['number']}]({p['url']})" for p in r["prs"])
        elif r.get("branches"):
            link = f"`{r['branches'][0]}`"
        lines.append(f"| {when} | {tool} | {text} | {link} |")
    return "\n".join(lines) + "\n"


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
        ledger.write_text(body)

    content = render_index(allrecs)
    index_file = out_dir / INDEX
    # A no-change sweep must not touch the tracked index: bumping only the
    # volatile "Swept ..." stamp dirties whichever checkout the Stop hook ran
    # in, so every worktree thread ended on an uncommitted-changes warning
    # (hit repeatedly through Mon 31 Aug 2026). Freshness already lives in the
    # gitignored watermark file.
    if (not index_file.exists()
            or _without_swept_stamp(index_file.read_text()) != _without_swept_stamp(content)):
        index_file.write_text(content)
    return fresh, len(allrecs)


# -----------------------------------------------------------------------------
# watermark
# -----------------------------------------------------------------------------


def _fingerprint(ledger: Path) -> tuple[int, str]:
    if not ledger.exists():
        return 0, ""
    raw = ledger.read_bytes()
    return raw.count(b"\n"), hashlib.sha256(raw).hexdigest()[:16]


def read_watermark(repo: Path) -> tuple[float, str]:
    """(since, reason). `since` is 0 whenever the checkout cannot be trusted.

    The watermark says "every source older than T is already recorded". That
    claim is only true of the ledger the watermark was WRITTEN against. A branch
    cut before a sweep commit, or a rebase that dropped one, leaves a fresh
    watermark sitting on a ledger that never received those rows, and the mtime
    filter then hides their sources forever. Fingerprinting the ledger turns
    that silent, permanent loss into a one-off full re-harvest.
    """
    wm_path = repo / OUT_DIRNAME / WATERMARK
    if not wm_path.exists():
        return 0.0, "no watermark: full harvest"
    try:
        wm = json.loads(wm_path.read_text())
        since = float(wm.get("swept_at", 0))
    except (json.JSONDecodeError, ValueError, TypeError, OSError):
        return 0.0, "unreadable watermark: full harvest"
    rows, digest = _fingerprint(repo / OUT_DIRNAME / LEDGER)
    if wm.get("ledger_rows") is None or wm.get("ledger_digest") is None:
        return 0.0, "watermark predates fingerprinting: full harvest"
    if rows < int(wm["ledger_rows"]) or digest != wm["ledger_digest"]:
        return 0.0, (
            f"ledger moved under the watermark ({wm['ledger_rows']} rows -> {rows}): "
            "checkout is stale, full harvest"
        )
    return since, "incremental"


def write_watermark(repo: Path) -> None:
    rows, digest = _fingerprint(repo / OUT_DIRNAME / LEDGER)
    (repo / OUT_DIRNAME / WATERMARK).write_text(json.dumps({
        "swept_at": datetime.now(UTC).timestamp(),
        "ledger_rows": rows,
        "ledger_digest": digest,
    }, indent=2))


def record_error(repo: Path, message: str) -> None:
    """Leave a readable trace of a refused write, without advancing freshness.

    The Stop hook sends stdout and stderr to /dev/null, so an abort that only
    printed would make the sweep a silent no-op forever -- the same class of
    invisible failure this whole change exists to remove. `--stats` reads this
    back, and the watermark is deliberately NOT advanced, so the next run
    retries rather than treating the skipped work as done.
    """
    wm_path = repo / OUT_DIRNAME / WATERMARK
    try:
        wm = json.loads(wm_path.read_text()) if wm_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        wm = {}
    wm["last_error"] = message
    wm["last_error_at"] = datetime.now(UTC).isoformat()
    wm_path.parent.mkdir(parents=True, exist_ok=True)
    wm_path.write_text(json.dumps(wm, indent=2))


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--full", action="store_true",
                    help="ignore the watermark and re-harvest")
    ap.add_argument("--stats", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        print(f"[ERROR] not a git repo: {repo}", file=sys.stderr)
        return 1

    since, why = (0.0, "--full") if args.full else read_watermark(repo)

    prompts = (
        harvest_claude(repo, since)
        + harvest_codex(repo, since)
        + harvest_cursor(repo, since)
    )

    if prompts:
        attach_links(prompts, branch_windows(repo), pr_map(repo))

    if args.stats:
        by: dict[str, int] = {}
        for p in prompts:
            by[p["tool"]] = by.get(p["tool"], 0) + 1
        prior = committed_rows(repo)
        print(f"[stats] {why} (since={since:.0f}) new prompts: {by or 'none'}; "
              f"{len(prior)} rows guarded from HEAD/trunk")
        wm_path = repo / OUT_DIRNAME / WATERMARK
        if wm_path.exists():
            try:
                last = json.loads(wm_path.read_text()).get("last_error")
            except (json.JSONDecodeError, OSError):
                last = None
            if last:
                print(f"[stats] last refused write: {last}")
        return 0

    try:
        fresh, total = write_out(repo, prompts, committed_rows(repo))
    except RegressionError as exc:
        record_error(repo, str(exc))
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    write_watermark(repo)
    print(f"[OK] {why}: +{fresh} new, {total} total -> {repo / INDEX_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
