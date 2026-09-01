"""Session-store harvesters for the prompt provenance sweep.

Reads what the HUMAN typed out of the three agent stores on disk - Claude Code
transcripts, Codex rollouts and the Cursor bubble table - and scopes each to one
repo. Kept apart from :mod:`scripts.provenance_sweep` so the code that decides
WHAT COUNTS AS A PROMPT is separable from the code that decides what is SAFE TO
WRITE; they fail in different ways and are read for different reasons.

Scoping is by the repo's MAIN worktree, never the caller's checkout. Claude Code
names its session directory after the cwd, so scoping to a linked worktree sees
only that worktree's sessions: measured Mon 31 Aug 2026 as 0 rows from a
worktree against 689 from the main checkout, which is how a Stop hook firing in
a worktree came to publish an index built from a stale ledger and nothing else.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

CC_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"
CURSOR_DB = (
    Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    / "globalStorage" / "state.vscdb"
)
NOISE_PREFIX = (
    "<system-reminder", "Caveat:", "<command-name", "<local-command",
    "<command-message", "<command-args", "<user-prompt-submit", "<teammate-message",
    "<recommended_plugins", "<function_results", "<task-notification",
    "[Request interrupted", "[SYSTEM NOTIFICATION", "IMPORTANT: Do NOT read files",
    # Agent-to-agent traffic, NOT a human typing:
    "<codex_delegation",   # Codex handing work to another Codex thread
    "<timestamp>",         # Cursor injects a system timestamp+context envelope
    "<environment", "<attached", "<context", "<additional_data",
)
# A prompt that opens with an XML-ish tag is machinery, not a person. the maintainer has
# never once opened a message with "<". This single rule caught 481 of the first
# 1113 harvested rows -- 436 Cursor envelopes and 39 Codex delegations.
MACHINE_OPENER = re.compile(r"^\s*<[a-zA-Z_][\w:-]*[\s>]")
def _clean(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip()
    if not t or t.startswith(NOISE_PREFIX) or MACHINE_OPENER.match(t):
        return None
    return t
def _iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            v = float(ts)
            if v > 1e11:  # milliseconds
                v /= 1000.0
            return datetime.fromtimestamp(v, UTC).isoformat()
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).isoformat()
    except (ValueError, OSError, OverflowError):
        return None
def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(("git", "-C", str(repo), *args), capture_output=True, text=True, check=False)
    return p.stdout if p.returncode == 0 else ""
def main_worktree(repo: Path) -> Path:
    """The primary checkout backing `repo`, which may be a linked worktree.

    Claude Code names its session directory after the cwd, so a worktree's
    sessions live under a DIFFERENT slug from the main checkout's. Scoping the
    harvest by the worktree's own path therefore sees only that worktree's
    sessions -- measured Mon 31 Aug 2026 as 0 rows from a fresh worktree against
    689 from the main path, which is how a Stop hook firing in a worktree came to
    rewrite the index from a stale ledger and nothing else.

    The main checkout's slug is a PREFIX of every worktree slug, so scoping by it
    covers the main checkout and all worktrees at once.
    """
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    if not common:
        return repo
    root = Path(common).parent
    return root if root.exists() else repo
def harvest_claude(repo: Path, since: float) -> list[dict]:
    """CC stores one dir per cwd, with the path slugified into the dir name."""
    slug = str(main_worktree(repo)).replace("/", "-")
    out: list[dict] = []
    if not CC_ROOT.exists():
        return out
    for d in CC_ROOT.iterdir():
        if not d.is_dir() or slug not in d.name:
            continue
        for f in d.glob("*.jsonl"):
            if f.stat().st_mtime <= since:
                continue
            for line in f.open(errors="ignore"):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, list):
                    text = "\n".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    text = content if isinstance(content, str) else ""
                text = _clean(text)
                if text:
                    out.append({
                        "tool": "claude-code", "ssid": f.stem, "source": str(f),
                        "at": _iso(rec.get("timestamp")), "text": text,
                    })
    return out
def harvest_codex(repo: Path, since: float) -> list[dict]:
    """Codex rollouts: event_msg/user_message, scoped by turn_context.cwd.

    A Codex thread run inside a worktree records that worktree as its cwd, so the
    match is a prefix of the main checkout, not equality.
    """
    out: list[dict] = []
    if not CODEX_ROOT.exists():
        return out
    target = str(main_worktree(repo))
    for f in CODEX_ROOT.rglob("*.jsonl"):
        if f.stat().st_mtime <= since:
            continue
        cwd, sid, pending = None, f.stem, []
        for line in f.open(errors="ignore"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = rec.get("payload") or {}
            if rec.get("type") == "turn_context" and payload.get("cwd"):
                cwd = payload["cwd"]
            elif rec.get("type") == "session_meta" and payload.get("session_id"):
                sid = payload["session_id"]
            elif payload.get("type") == "user_message":
                text = _clean(payload.get("message"))
                if text:
                    pending.append({
                        "tool": "codex", "ssid": sid, "source": str(f),
                        "at": _iso(rec.get("timestamp")), "text": text,
                    })
        if cwd and (cwd == target or cwd.startswith(target + "/")):
            out.extend(pending)
    return out
def harvest_cursor(repo: Path, since: float) -> list[dict]:
    """Cursor: bubbleId:<composer>:<bubble> rows, type 1 == user, 2 == assistant.

    Opened immutable so a running Cursor cannot be disturbed and its lock cannot
    block us.
    """
    out: list[dict] = []
    if not CURSOR_DB.exists() or CURSOR_DB.stat().st_mtime <= since:
        return out
    name = main_worktree(repo).name
    try:
        con = sqlite3.connect(f"file:{CURSOR_DB}?immutable=1", uri=True)
    except sqlite3.Error:
        return out
    try:
        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        )
        for key, value in rows:
            if not value:
                continue
            try:
                b = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            if b.get("type") != 1:
                continue
            text = _clean(b.get("text"))
            if not text:
                continue
            blob = value if isinstance(value, str) else ""
            # Cursor bubbles carry no cwd; scope by repo name appearing in the
            # message or its attached context. Coarse, and declared as such.
            if name not in blob and name not in text:
                continue
            parts = key.split(":")
            out.append({
                "tool": "cursor", "ssid": parts[1] if len(parts) > 2 else key,
                "source": f"cursorDiskKV/{key}", "at": None, "text": text,
                "scope_confidence": "name-match",
            })
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out
