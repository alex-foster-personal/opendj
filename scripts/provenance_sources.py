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

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath

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
    # Wrapped envelopes. MACHINE_OPENER only catches traffic that OPENS with a
    # tag; Claude Code prefixes cross-session traffic with a sentence first, so
    # the tag is no longer in first position and the row read as a human prompt.
    # The 17 Aug ledger already carried these, which is how they were found.
    "Another Claude session sent a message",
    "This came from another Claude session",
)
# A prompt that opens with an XML-ish tag is machinery, not a person. the maintainer has
# never once opened a message with "<". This single rule caught 481 of the first
# 1113 harvested rows -- 436 Cursor envelopes and 39 Codex delegations.
MACHINE_OPENER = re.compile(r"^\s*<[a-zA-Z_][\w:-]*[\s>]")

# How many characters of a prompt identify it. The ledger was built at 80, so
# widening it would re-admit rows the existing file already deduplicated away.
# Lives here, with the definition of what a prompt IS, because both the sweep's
# `row_key` and the index's `_comparable` must ask identity at the same
# granularity - and importing it from either of them would make them cyclic.
KEY_PREFIX = 80
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
def in_repo_scope(cwd: str | None, main: Path, live_roots: frozenset[str]) -> bool:
    """Does a session recorded at `cwd` belong to THIS repo?

    Decided from the transcript's OWN recorded cwd, never from the session
    directory's name. Claude Code slugifies the cwd into that directory name, and
    a substring test on it accepts any unrelated checkout whose path merely
    contains this repo's path - `/w/music-dj-tools-private` would have written
    its prompts into this tracked ledger.

    Three ways a cwd qualifies, in descending order of certainty:

    - it is the main worktree, or under it;
    - it still exists and git reports the same common dir, which covers every
      LIVE linked worktree whether or not it is currently registered;
    - it no longer exists AND its name carries this repo's worktree marker,
      `<main>-wt-`. Refusing retired paths outright would drop real history: 3
      prompts from the retired `-wt-cdj-berlin` worktree are reachable only this
      way. Flagged `retired-path` so the weaker claim stays visible.

    The marker, rather than a bare `<main>-` prefix, is the whole point. Deleting
    a checkout does NOT delete its transcript, so "it no longer exists" says
    nothing about whether it was ever ours: a since-deleted sibling such as
    `music-dj-tools-private` matches a bare prefix, and its private prompts would
    have entered a git-TRACKED ledger. CLAUDE.md fixes worktrees at
    `../music-dj-tools-wt-<slug>`, so `-wt-` is durable evidence of this repo's
    own convention where mere adjacency in the filesystem is not. Anything else
    that no longer exists fails closed and is simply not harvested.
    """
    return repo_scope_reason(cwd, main, live_roots) is not None


#: How a cwd earned its place, strongest first. Only the last is a weaker
#: claim, and only it may be stamped on a row.
ADMITTED_MAIN = "main-worktree"
ADMITTED_LIVE = "live-worktree"
ADMITTED_RETIRED = "retired-path"


def repo_scope_reason(cwd: str | None, main: Path, live_roots: frozenset[str]) -> str | None:
    """WHY a cwd belongs to this repo, or None if it does not.

    The reason exists because the harvesters cannot re-derive it. They stamped
    `retired-path` on any admitted row whose cwd was missing from disk, which
    is a different question: a deleted subdirectory of the main checkout -
    `<repo>/tmp/build` after a cleanup - is admitted by definitive LEXICAL
    containment and has nothing to do with the worktree-marker fallback, yet
    the ledger presented it as if it did. Codex found it on #708. Returning the
    reason is the fix rather than a second existence test, because the reason
    is what the caller actually wants and only this function knows it.
    """
    if not cwd:
        return None
    here = Path(cwd)
    if _within(here, main):
        return ADMITTED_MAIN
    for live in live_roots:
        if _within(here, Path(live)):
            return ADMITTED_LIVE
    if _is_on_this_machine(here):
        return None
    return ADMITTED_RETIRED if _is_retired_worktree_of(here, main) else None


def _is_on_this_machine(here: Path) -> bool:
    """Is `here` present on this host, when it may not be readable?

    `Path.exists()` returns False for a path that is missing and RAISES for one
    whose parent denies traversal. That raise leaves this function, reaches the
    `except OSError` in `provenance_cli`, and refuses the WHOLE sweep with the
    watermark left where it was, so every later run refuses in the same place.
    Permanently, on a machine that keeps the unreadable directory.

    Found on agentbox, Thu 3 Sep 2026: a self-hosted CI runner holding a
    restored macOS home at `/Users/user` (mode 750, another uid) against
    transcripts whose recorded cwd is `/Users/user/code/afmac`. Ten provenance
    tests failed on it, none of them naming a permission.

    A path this process cannot traverse is PRESENT and not ours, which is what
    this returns. That is not a guess dressed up as a fact: the harvest reads
    the checkout it runs in, so a directory it cannot enter is not a live
    worktree of this repo, and it is the conservative arm besides - the
    alternative branch invents `retired-path` provenance for a cwd nobody on
    this host can see. Narrow on purpose: only EACCES is answered this way, and
    any other OSError is still a defect that must surface.
    """
    try:
        return here.exists()
    except PermissionError:
        return True


def _within(cwd: PurePath, root: PurePath) -> bool:
    """Is `cwd` the root itself, or below it?

    Path containment rather than `cwd.startswith(str(root) + "/")`, which was a
    silent data loss on Windows: a session recorded at
    `C:\\work\\music-dj-tools\\apps\\webui` never matches a separator the host
    does not write, so every prompt typed below the checkout was rejected as
    out of scope AND the watermark advanced past it, making the loss
    unrecoverable without a manual --full. bifrost2 runs this from the Stop
    hook, so nobody would have seen it. Codex found it on #708, one door along
    from the `import fcntl` defect with the same blast radius.

    Comparison is native-separator by construction: the harvest reads the store
    belonging to the machine it runs on, so a Windows cwd is parsed by
    WindowsPath and a POSIX one by PosixPath. Typed `PurePath` so the rule can
    be driven with `PureWindowsPath` from a macOS test.
    """
    return cwd == root or root in cwd.parents


def _is_retired_worktree_of(cwd: PurePath, main: PurePath) -> bool:
    """Was `cwd` a linked worktree of this repo, judged by name alone?

    Only reached for a path that no longer exists, where nothing but the name
    survives. CLAUDE.md fixes worktrees at `../<main>-wt-<slug>`, so the test is
    "a sibling of the main checkout whose name carries the marker" - expressed
    as a parent/name comparison rather than a string prefix for the same
    separator reason as `_within`.
    """
    marker = main.name + RETIRED_WORKTREE_MARKER
    candidates = (cwd, *cwd.parents)
    return any(c.parent == main.parent and c.name.startswith(marker) for c in candidates)


#: What a linked worktree of THIS repo is named, per CLAUDE.md
#: (`../music-dj-tools-wt-<slug>`). Load-bearing for retired paths: it is the
#: only durable evidence left once the directory itself is gone.
RETIRED_WORKTREE_MARKER: str = "-wt-"


# Every character Claude Code will not put in a store directory name. Derived
# from evidence, not from a reading of the separator set: 197 real store
# directories captured off two machines (39 Windows, 158 POSIX) use exactly
# `[A-Za-z0-9-]` and nothing else, and the capture is committed as
# tests/fixtures/provenance/cc-store-names.tsv.
_CC_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9-]")


def _cc_slug(path: Path | str) -> str:
    """Claude Code's store-directory name for a cwd: everything unsafe to a dash.

    Codex found this twice on #708 and was right twice, but the second finding
    was the general case of the first. The original replaced `/` only; the
    round after that replaced `\\` too; both were still enumerating separators,
    and the class is wider than separators. The real encoding dashes EVERY
    character outside `[A-Za-z0-9-]`, which is why `D:\\code\\x` is stored as
    `D--code-x` rather than the impossible `D:-code-x` a colon-preserving slug
    produces, and why this repo`s own `.claude/worktrees/<slug>` sessions are
    stored under `...--claude-worktrees-<slug>` rather than `...-.claude-...`.

    Enumerating characters was the wrong move, so this does not enumerate: it
    keeps the safe set and dashes the rest. The safe set is measured from the
    capture rather than assumed, and the test asserts the measurement.

    What the earlier versions cost, on every affected path: the substring
    prefilter at the call site rejects every store directory, `harvest_claude`
    returns nothing, the sweep reports a clean run, and the watermark advances
    over prompts that were never read. Silent and permanent, which is the exact
    failure this tool exists to prevent. It was not Windows-only either - any
    checkout path holding a dot or an underscore hit it, including every
    `.claude/worktrees` session in this repository.

    `os.sep` is still not used: the slug describes how a store on disk was
    NAMED, a property of the machine that wrote it, not of the machine reading
    it. Normalizing unconditionally is what lets a capture cross platforms.
    """
    return _CC_SLUG_UNSAFE.sub("-", str(path))


def live_worktree_roots(repo: Path) -> frozenset[str]:
    """Every checkout git currently reports for this repository."""
    out = set()
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            out.add(line[len("worktree "):].strip())
    return frozenset(out)


def _cc_text(rec: dict) -> str:
    """The typed text of one Claude Code user record, list or plain form."""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return content if isinstance(content, str) else ""


def harvest_claude(repo: Path, since: float, cc_root: Path | None = None) -> list[dict]:
    """CC stores one dir per cwd, with the path slugified into the dir name.

    The directory name is a cheap PREFILTER only. Every record is then admitted
    or rejected on its own `cwd` field, which all 12,373 user records in this
    repo's stores were measured to carry.

    It prefilters on the main worktree's slug AND every live worktree's, because
    a linked worktree is not required to sit under the main path: this repo has
    live checkouts under `~/.codex/worktrees/` whose slug shares no prefix with
    the main one, and matching the main slug alone silently dropped every prompt
    typed in them. Scope is still decided per record, so widening the prefilter
    cannot admit anything `in_repo_scope` would reject.

    `cc_root` is a parameter rather than a module global read at call time so a
    test can point the production path at a captured fixture store without
    monkeypatching, which this repo's AGENTS.md contract forbids.
    """
    cc_root = CC_ROOT if cc_root is None else cc_root
    main = main_worktree(repo)
    live = live_worktree_roots(repo)
    slugs = {_cc_slug(main)} | {_cc_slug(root) for root in live}
    out: list[dict] = []
    if not cc_root.exists():
        return out
    for d in cc_root.iterdir():
        if not d.is_dir() or not any(sl in d.name for sl in slugs):
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
                cwd = rec.get("cwd")
                admission = repo_scope_reason(cwd, main, live)
                if admission is None:
                    continue
                text = _clean(_cc_text(rec))
                if text:
                    row = {
                        "tool": "claude-code", "ssid": f.stem, "source": str(f),
                        "at": _iso(rec.get("timestamp")), "text": text,
                    }
                    if admission == ADMITTED_RETIRED:
                        row["scope_confidence"] = ADMITTED_RETIRED
                    out.append(row)
    return out


def harvest_codex(repo: Path, since: float, codex_root: Path | None = None) -> list[dict]:
    """Codex rollouts: event_msg/user_message, scoped by turn_context.cwd.

    Scoped PER TURN, not once for the whole rollout. A rollout resumed from a
    different checkout carries several turn contexts, and classifying the file by
    its LAST one either imports every earlier unrelated prompt or discards every
    valid one, depending only on where the final turn happened to run.
    """
    codex_root = CODEX_ROOT if codex_root is None else codex_root
    out: list[dict] = []
    if not codex_root.exists():
        return out
    main = main_worktree(repo)
    live = live_worktree_roots(repo)
    for f in codex_root.rglob("*.jsonl"):
        if f.stat().st_mtime <= since:
            continue
        cwd, sid = None, f.stem
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
            elif payload.get("type") == "user_message" and (
                admission := repo_scope_reason(cwd, main, live)
            ):
                text = _clean(payload.get("message"))
                if text:
                    row = {
                        "tool": "codex", "ssid": sid, "source": str(f),
                        "at": _iso(rec.get("timestamp")), "text": text,
                    }
                    # Same marker the Claude path sets, for the same reason: a
                    # cwd that no longer exists was admitted on the "-wt-"
                    # naming convention alone, and a row admitted on a name
                    # must not read in the ledger as one admitted on a path
                    # that was actually there. Keyed on the ADMISSION, not on
                    # whether the path exists: a deleted subdirectory of a
                    # registered worktree is admitted lexically and is not this.
                    if admission == ADMITTED_RETIRED:
                        row["scope_confidence"] = ADMITTED_RETIRED
                    out.append(row)
    return out


def harvest_cursor(repo: Path, since: float, cursor_db: Path | None = None) -> list[dict]:
    """Cursor: bubbleId:<composer>:<bubble> rows, type 1 == user, 2 == assistant.

    Opened immutable so a running Cursor cannot be disturbed and its lock cannot
    block us.
    """
    cursor_db = CURSOR_DB if cursor_db is None else cursor_db
    out: list[dict] = []
    if not cursor_db.exists() or cursor_db.stat().st_mtime <= since:
        return out
    name = main_worktree(repo).name
    # No `except sqlite3.Error: return out` here any more, and none around the
    # read below. Swallowing either turned an UNREADABLE store into an EMPTY
    # one, and the caller then banked a watermark over prompts it never saw.
    # `cursor_store_defect` is the place that answers "can this be read", it
    # runs before the sweep takes its lock, and it opens the database exactly
    # this way - so an error arriving HERE is new information that appeared
    # after the probe cleared, which is precisely the case that must be loud.
    con = sqlite3.connect(f"file:{cursor_db}?immutable=1", uri=True)
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
    finally:
        con.close()
    return out


# ---------------------------------------------------------------- source roots


@dataclass(frozen=True)
class SourceRoots:
    """Where the three harvesters read from.

    Defaults are the real per-user stores, so production callers construct
    nothing and behave exactly as before. A caller that wants a DIFFERENT
    store names it, the same shape as the app's `--data-dir`: the choice
    arrives as an argument instead of a caller reaching in and reassigning a
    module global. That distinction is what lets a test point the sweep at a
    directory it created without manufacturing application state - the
    production harvesters still run, over stores that are really there.
    """

    claude: Path = CC_ROOT
    codex: Path = CODEX_ROOT
    cursor_db: Path = CURSOR_DB

    def __post_init__(self) -> None:
        """Canonicalize every root, so a store's IDENTITY is not its spelling.

        `identity()` used to hash the strings as given while the CLI never
        resolved them, so `--claude-root sessions` run from two different
        working directories produced ONE token for TWO stores. A watermark
        written under directory A was then honoured under directory B and
        every source in B older than that cutoff was skipped indefinitely -
        the same permanent, silent loss the token was added to prevent,
        reached by a relative path instead of a missing flag. Codex found it
        on #708, one round after the token landed.

        Resolving here rather than at each use site is deliberate: harvesting
        and identity must agree about which store they mean, and a rule
        applied in one of two places is the defect itself. `resolve()` also
        collapses symlinks, so two spellings of one real store now share a
        token, which is correct - it is the same store.
        """
        for name in ("claude", "codex", "cursor_db"):
            path = Path(getattr(self, name)).expanduser().resolve()
            object.__setattr__(self, name, path)

    def identity(self) -> str:
        """A short stable token for WHICH stores a sweep read.

        The watermark says "every source older than T is already recorded".
        That is a claim about particular stores, and once the roots became
        configuration it stopped being a claim about the same ones every time:
        a run against an empty `--claude-root` wrote a cutoff that the next
        default-root run then honoured, so real transcripts older than it were
        skipped indefinitely. Nothing surfaced it, because both runs succeeded.
        Codex found it on #708, one round after the flags were added.

        Stamping this alongside the cutoff makes the watermark self-healing in
        the same way the ledger fingerprint already is: a watermark whose
        sources do not match this run's is simply not honoured, and the run
        re-harvests in full. That is a one-off cost, not a loss.

        Hashed rather than stored verbatim because the paths are per-user
        absolute paths and the watermark is not the place to publish someone's
        home directory layout. Truncated to 16 hex characters, matching the
        ledger digest beside it - this is a change detector, not a security
        boundary.
        """
        raw = "\n".join(str(p) for p in (self.claude, self.codex, self.cursor_db))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ----------------------------------------------------------------- usability
#
# EXISTENCE IS NOT USABILITY, and the sweep's whole safety argument rests on
# the difference.
#
# The first version of this guard asked `path.exists()`. That covers a
# misspelled flag and nothing else: `--codex-root` aimed at a regular file, or
# `--cursor-db` at a zero-byte file, both EXIST, both harvest zero rows, and
# the run then advances the shared watermark past sources it never read - the
# permanent, silent loss the guard was added to prevent, one door along.
# Codex found it on #708 and cited the proof from inside this repo: the CLI
# test creates its Cursor database with `.touch()`, `harvest_cursor` swallowed
# the resulting sqlite error, and the sweep exited 0.
#
# So the probes below answer "can this store be READ", not "is something
# there". They are the presence-of-the-good-thing form of the same question
# (.claude/rules/verification.md), and each returns a REASON rather than a
# bool so the refusal can name what is wrong with which store.

CURSOR_TABLE = "cursorDiskKV"


def directory_store_defect(path: Path) -> str | None:
    """Why this session-file store cannot be read, or None if it can be.

    Claude Code and Codex both keep their transcripts as files under a
    directory, so "usable" is the same question for both: a directory we are
    allowed to enter and list.
    """
    if not path.is_dir():
        return "exists but is not a directory"
    if not os.access(path, os.R_OK | os.X_OK):
        return "is a directory this process may not read"
    return None


def cursor_store_defect(path: Path) -> str | None:
    """Why the Cursor database cannot be read, or None if it can be.

    Opened the same way `harvest_cursor` opens it - immutable, by URI - so a
    store that passes this probe is a store that harvester can read. Probing
    with a DIFFERENT connection mode would answer a different question than
    the one the caller is about to ask.

    The table query is the load-bearing half. A file can be valid sqlite and
    still not be a Cursor store, and an empty-but-valid database is a true
    zero while a schema-less one is unmeasured. Selecting one row from the
    table the harvester reads is what separates them.
    """
    if not path.is_file():
        return "exists but is not a regular file"
    try:
        con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    except sqlite3.Error as exc:
        return f"cannot be opened as a sqlite database ({exc})"
    try:
        # CURSOR_TABLE is a module constant, not caller input; parameter
        # binding is not available for identifiers.
        con.execute(f"SELECT 1 FROM {CURSOR_TABLE} LIMIT 1").fetchall()
    except sqlite3.Error as exc:
        return f"is not a Cursor store: no readable {CURSOR_TABLE} ({exc})"
    finally:
        con.close()
    return None


def harvest_all(repo: Path, since: float, roots: SourceRoots | None = None) -> list[dict]:
    """Every store, in one call, so callers cannot harvest two of the three.

    Both sweep entry points (`--stats` and the locked write path) used to spell
    this sum out separately, which is how they drifted apart on #708.
    """
    roots = SourceRoots() if roots is None else roots
    return (
        harvest_claude(repo, since, roots.claude)
        + harvest_codex(repo, since, roots.codex)
        + harvest_cursor(repo, since, roots.cursor_db)
    )
