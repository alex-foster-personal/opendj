"""Proposal store, config and key for the "fix broken track metadata" admin feature.

Mini-PRD
--------
✔︎ S1 A scan's verdicts persist as PROPOSALS in a sidecar sqlite file next to state.db
   (``<state_dir>/title-fix.db``), each carrying the original row values, the proposal, and
   its provenance (scan id, fixer status, source, confidence, cost). Nothing here touches a
   track: the store records, the service applies.
   [if] a proposal is stored then original_title/original_artists_json read back verbatim [else ⛔️]
   [if] a second scan proposes for the same track then the older PENDING row is superseded [else ⛔️]
   [if] a proposal with no change is stored then its state is noop, never pending [else ⛔️]
✔︎ S2 Config is a small JSON file (``<state_dir>/title-fix-config.json``); unknown or invalid
   values raise, never coerce.
   [if] auto_apply_policy is "sometimes" then ConfigError [else ⛔️]
   [if] the file is absent then the CFG defaults are returned (feature disabled) [else ⛔️]
✔︎ S3 The user's OpenRouter key lives in its own owner-only file in the data dir (the
   ``sync-credential`` pattern), never in the config JSON and never in a response. A user key
   beats the environment key; the build-provided key is out of scope (TODO below).
   [if] a user key is saved then key_status().source == "user" and the hint shows 4 chars [else ⛔️]
   [if] neither user nor env key exists then key_status().source == "none" [else ⛔️]

TODO(build-key): a build-provided OpenRouter key is injected at build time from Doppler and
never committed. Its resolution slot is between the user key and the environment here.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from apps.shared.private_files import is_owner_only, restrict_to_owner

from .title_fix import VERSION_TAGS, Track, Verdict
from .title_fix_rows import (  # noqa: F401  # re-exported: the row types are part of the store's surface
    PRODUCER_NOTE_PREFIX,
    BatchRow,
    ProposalFilter,
    ProposalRow,
    ScanRow,
    _batch_from_row,
    _proposal_from_row,
    _scan_from_row,
)


class CFG:
    SCHEMA_VERSION = 4  # PRAGMA user_version once the schema and column migrations are applied; bump with a new migration
    BUSY_TIMEOUT_S = 5.0  # how long a store connection waits on another writer before raising "database is locked"
    SQL_IN_CHUNK = 500  # ids per IN (...): under SQLite's default 999 variable cap, as apps/shared/state/queries.py
    STORE_FILENAME = "title-fix.db"
    CONFIG_FILENAME = "title-fix-config.json"
    KEY_FILENAME = "title-fix-openrouter-key"  # in the DATA dir (state_dir.parent), like sync-credential
    OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
    DEFAULT_ENABLED = False  # a scan spends money: opt in
    DEFAULT_POLICY = "off"  # non-destructive by default: nothing applies without a click
    DEFAULT_SPEND_CAP_USD = 0.50
    SPEND_CAP_MIN_USD = 0.01
    SPEND_CAP_MAX_USD = 100.0
    TAG_FAMILIES = ("version", "context", "cover", "project")
    KEY_HINT_CHARS = 4
    KEY_HINT_MIN_LEN = 16  # a key shorter than this shows no characters at all (4 of 5 would be most of it)


AutoApplyPolicy = Literal["off", "safe_tier", "gate_approved"]
POLICIES: tuple[str, ...] = ("off", "safe_tier", "gate_approved")
ProposalState = Literal["pending", "applied", "rejected", "superseded", "stale", "undone", "noop"]
ScanState = Literal["running", "done", "capped", "failed"]
SAFE_TIER_SOURCES = frozenset({"safe-tier"})  # title_fix round 6 names its no-LLM tier this way


class BatchReleasedError(RuntimeError):
    """An apply tried to write into a batch that was reconciled away (or undone): its claims are no longer its own."""


class ScanLeaseLostError(RuntimeError):
    """A scan worker wrote after its lease ended (taken over as stale, or finished): it must stop, not write."""


class FenceExitError(RuntimeError):
    """The fenced block finished, state.db included, and only the fence's own sidecar commit then failed. What the
    block wrote stands; the caller must keep its batch or intent for reconciliation, never release it. batch_id, when
    set by a batching caller, names the undo batch recording the write."""

    batch_id: str | None = None
    applied: tuple[str, ...] = ()  # the proposal ids the committed write covered


class StoreBusyError(RuntimeError):
    """The store's write lock is held by a live writer and did not free within the wait."""


class UndoIntentLostError(RuntimeError):
    """An undo resumed after its recorded intent was cleared and taken by another undo: it must not write."""


class ConfigError(ValueError):
    """A config value is invalid; nothing was written."""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


#: Row-level source / provenance tags (apps/tags/title_fix.py provenance_tags) plus the release-context tags: all of
#: them are the "context" family, so switching that family off stops every one of them being written.
CONTEXT_TAGS = frozenset({
    "soundtrack", "unreleased", "mik-analyzed", "low-bitrate", "streaming-only", "youtube-rip", "beatport-sourced",
    "duplicate-copy", "video-source", "live-set", "dj-mix",
})


#: Version tags are the names VERSION_TAGS derives from titles, plus the JEV-judged "mashup".
VERSION_FAMILY_TAGS = frozenset({name for name, _ in VERSION_TAGS} | {"mashup"})


def tag_family(tag: str) -> str:
    """Which configurable family a fixer tag belongs to."""
    if tag.startswith("dj-project"):
        return "project"
    if tag in ("cover", "karaoke") or tag.endswith("-cover"):
        return "cover"
    if tag in CONTEXT_TAGS:
        return "context"
    if tag in VERSION_FAMILY_TAGS:
        return "version"
    raise ValueError(f"tag {tag!r} has no declared family: add it to CONTEXT_TAGS or the version tags")


# ----------------------------------------------------------------- paths


@dataclass(frozen=True)
class StorePaths:
    state_db: Path

    @property
    def store_db(self) -> Path:
        return self.state_db.parent / CFG.STORE_FILENAME

    @property
    def config(self) -> Path:
        return self.state_db.parent / CFG.CONFIG_FILENAME

    @property
    def key(self) -> Path:
        # the library DATA dir, by the same layout rule as shell_output_health.data_dir_from_state_db (apps/tags may
        # not import the webui): data_dir/state/state.db is nested, anything else is flat beside the database
        data_dir = self.state_db.parent.parent if self.state_db.parent.name == "state" else self.state_db.parent
        return data_dir / CFG.KEY_FILENAME


# ----------------------------------------------------------------- config


@dataclass(frozen=True)
class TitleFixConfig:
    enabled: bool = CFG.DEFAULT_ENABLED
    auto_apply_policy: str = CFG.DEFAULT_POLICY
    spend_cap_usd: float = CFG.DEFAULT_SPEND_CAP_USD
    tag_families: tuple[str, ...] = CFG.TAG_FAMILIES

    def validated(self) -> TitleFixConfig:
        if not isinstance(self.enabled, bool):
            raise ConfigError("enabled must be a boolean")
        if self.auto_apply_policy not in POLICIES:
            raise ConfigError(f"auto_apply_policy must be one of {POLICIES}, got {self.auto_apply_policy!r}")
        cap = self.spend_cap_usd
        if isinstance(cap, bool) or not isinstance(cap, int | float):
            raise ConfigError("spend_cap_usd must be a number")
        if not CFG.SPEND_CAP_MIN_USD <= cap <= CFG.SPEND_CAP_MAX_USD:
            raise ConfigError(f"spend_cap_usd must be between {CFG.SPEND_CAP_MIN_USD} and {CFG.SPEND_CAP_MAX_USD}")
        unknown = [f for f in self.tag_families if f not in CFG.TAG_FAMILIES]
        if unknown:
            raise ConfigError(f"unknown tag families {unknown}; known: {CFG.TAG_FAMILIES}")
        return TitleFixConfig(self.enabled, self.auto_apply_policy, float(cap), tuple(dict.fromkeys(self.tag_families)))


def load_config(paths: StorePaths) -> TitleFixConfig:
    if not paths.config.exists():
        return TitleFixConfig()
    try:
        raw = json.loads(paths.config.read_text(encoding="utf-8"))
    except ValueError as exc:  # truncated or otherwise invalid JSON (also a bad UTF-8 byte): name the file to repair
        raise ConfigError(f"{paths.config} is not valid JSON ({exc}); repair or delete it") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{paths.config} is not a JSON object")
    known = {"enabled", "auto_apply_policy", "spend_cap_usd", "tag_families"}
    if unknown := set(raw) - known:
        raise ConfigError(f"{paths.config} has unknown keys {sorted(unknown)}")
    if missing := known - set(raw):  # save_config always writes every key: a partial file is damage, not "use defaults"
        raise ConfigError(f"{paths.config} is missing keys {sorted(missing)}; repair or delete it to restore the defaults")
    base = TitleFixConfig()
    families = raw.get("tag_families", base.tag_families)
    if not isinstance(families, (list, tuple)):
        raise ConfigError(f"{paths.config}: tag_families must be a list, got {type(families).__name__}")
    return TitleFixConfig(
        enabled=raw.get("enabled", base.enabled),
        auto_apply_policy=raw.get("auto_apply_policy", base.auto_apply_policy),
        spend_cap_usd=raw.get("spend_cap_usd", base.spend_cap_usd),
        tag_families=tuple(families),
    ).validated()


def save_config(paths: StorePaths, config: TitleFixConfig) -> TitleFixConfig:
    valid = config.validated()
    payload = {**asdict(valid), "tag_families": list(valid.tag_families)}
    _atomic_write(paths.config, json.dumps(payload, indent=2) + "\n", owner_only=False)
    return valid


# ----------------------------------------------------------------- key


@dataclass(frozen=True)
class KeyStatus:
    configured: bool
    source: Literal["user", "env", "none"]
    hint: str | None  # last CFG.KEY_HINT_CHARS chars only; never the key


def _hint(key: str) -> str:
    """The last KEY_HINT_CHARS characters of a key long enough that they are a small part of it; a short key is fully
    opaque, so the hint can never reveal the secret itself."""
    if len(key) < CFG.KEY_HINT_MIN_LEN:
        return "..."
    return "..." + key[-CFG.KEY_HINT_CHARS :]


def _read_user_key(paths: StorePaths) -> str | None:
    if not paths.key.exists():
        return None
    if not is_owner_only(paths.key):
        raise ConfigError(
            f"the API key file {paths.key} is readable by other accounts; restrict it to this account (chmod 600 on "
            "POSIX, owner-only ACL on Windows) or save the key again from the Title Fix tab"
        )
    key = paths.key.read_text(encoding="utf-8").strip()
    if not key:  # a saved-but-empty key is a broken save, never a silent fall back to the environment key
        raise ConfigError(f"the API key file {paths.key} is empty; save the key again or clear it from the Title Fix tab")
    return key


def resolve_key(paths: StorePaths) -> tuple[str | None, KeyStatus]:
    """The effective key (user file > environment) and its masked status."""
    user = _read_user_key(paths)
    if user:
        return user, KeyStatus(True, "user", _hint(user))
    env = os.environ.get(CFG.OPENROUTER_KEY_ENV, "").strip()
    if env:
        return env, KeyStatus(True, "env", _hint(env))
    return None, KeyStatus(False, "none", None)


def save_user_key(paths: StorePaths, key: str) -> KeyStatus:
    key = key.strip()
    if not key or any(c.isspace() for c in key):
        raise ConfigError("the API key must be a single non-empty token")
    _atomic_write(paths.key, key, owner_only=True)
    return resolve_key(paths)[1]


def clear_user_key(paths: StorePaths) -> KeyStatus:
    paths.key.unlink(missing_ok=True)
    return resolve_key(paths)[1]


def _atomic_write(target: Path, text: str, *, owner_only: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if owner_only:
            restrict_to_owner(Path(tmp_name))
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


# ----------------------------------------------------------------- store

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    scan_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state TEXT NOT NULL CHECK (state IN ('running','done','capped','failed')),
    requested INTEGER NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    spend_cap_usd REAL NOT NULL,
    error TEXT,
    auto_batch_id TEXT,
    heartbeat_at TEXT NOT NULL,
    auto_apply_policy TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scans(scan_id),
    stable_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    original_title TEXT NOT NULL,
    original_artist TEXT NOT NULL,
    original_artists_json TEXT NOT NULL,
    proposed_title TEXT NOT NULL,
    proposed_artist TEXT NOT NULL,
    original_artist_credit TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    fixer_status TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL,
    jev_kind TEXT NOT NULL,
    notes_json TEXT NOT NULL,
    error TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','applied','rejected','superseded','stale','undone','noop')),
    batch_id TEXT,
    tags_added_json TEXT NOT NULL DEFAULT '[]',
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS proposals_state ON proposals(state, fixer_status);
CREATE INDEX IF NOT EXISTS proposals_stable_id ON proposals(stable_id);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('manual','auto')),
    policy TEXT NOT NULL,
    proposal_count INTEGER NOT NULL,
    undone_at TEXT,
    committed_at TEXT,
    undo_started_at TEXT,
    undo_targets_json TEXT,
    applied_state_json TEXT
);
"""


















# Columns added after the first shipped schema. CREATE TABLE IF NOT EXISTS never alters an existing table,
# so a store file written by an older build gets them here. (table, column, DDL, backfill SQL or None)
_ADDED_COLUMNS: tuple[tuple[str, str, str, str | None], ...] = (
    ("scans", "auto_apply_policy", "TEXT NOT NULL DEFAULT ''", None),  # the policy the scan started under (settings can move)
    ("scans", "heartbeat_at", "TEXT NOT NULL DEFAULT ''", None),  # '' sorts before any cutoff: an old running row is stale
    ("batches", "committed_at", "TEXT", "UPDATE batches SET committed_at = created_at"),  # old batches were already written
    ("batches", "undo_started_at", "TEXT", None),
    ("batches", "undo_targets_json", "TEXT", None),  # the proposal ids a running undo restores (a partial undo skips some)
    ("batches", "applied_state_json", "TEXT", None),  # proposal_id -> the track ETag and tags as of the write
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, ddl, backfill in _ADDED_COLUMNS:
        if column in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        if backfill is not None:
            conn.execute(backfill)
    _migrate_legacy_etags(conn)


def _migrate_legacy_etags(conn: sqlite3.Connection) -> None:
    """The earlier v2 build recorded only ETags (applied_etags_json). Keep them as a baseline with no tag set, so Undo
    still refuses a row edited since the apply instead of treating the batch as having no baseline."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(batches)")}
    if "applied_etags_json" not in columns:
        return
    conn.execute(
        "UPDATE batches SET applied_state_json = (SELECT json_group_object(key, json_object('etag', value, 'tags', NULL)) "
        "FROM json_each(applied_etags_json)) WHERE applied_etags_json IS NOT NULL AND applied_state_json IS NULL"
    )


#: A scan that died after its auto-apply committed (the scan row is a separate write) keeps its direct link to the
#: undoable batch: the committed auto batch that claimed this scan's proposals.
_RECOVER_AUTO_BATCH = (
    "auto_batch_id = COALESCE(auto_batch_id, (SELECT b.batch_id FROM proposals p JOIN batches b ON b.batch_id = p.batch_id "
    "WHERE p.scan_id = scans.scan_id AND b.kind = 'auto' AND b.committed_at IS NOT NULL ORDER BY b.created_at LIMIT 1))"
)




def _notes_with_producer(verdict: Verdict) -> list[str]:
    """A producer credit the proposal removes from the title has no track field to move to, so it is recorded on the
    proposal itself: the credit is never only in a title the apply is about to rewrite."""
    return [*verdict.notes, *([f"{PRODUCER_NOTE_PREFIX} {verdict.producer}"] if verdict.producer else [])]


def chunked(ids: list[str], size: int) -> Iterator[list[str]]:
    """Consecutive slices of at most ``size`` ids."""
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


class ProposalStore:
    """Thin sqlite wrapper. One connection per call: the daemon and the CLI may share the file."""

    def __init__(self, paths: StorePaths) -> None:
        self.paths = paths
        with self._conn() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > CFG.SCHEMA_VERSION:
                raise ConfigError(
                    f"{self.paths.store_db.name} is schema v{version}, newer than this build's v{CFG.SCHEMA_VERSION}; "
                    "refusing to open (or downgrade) it"
                )
            if version == CFG.SCHEMA_VERSION:
                return  # already initialised: a status read must not need the write lock a live apply is holding
            conn.executescript(SCHEMA)
            _add_missing_columns(conn)
            # A store written by the earlier, non-atomic scan start may hold several running rows: keep the newest
            # as the lease holder and fail the rest, or the unique index below cannot be created.
            conn.execute(
                "UPDATE scans SET state = 'failed', finished_at = ?, error = 'superseded duplicate running scan' "
                "WHERE state = 'running' AND scan_id NOT IN "
                "(SELECT scan_id FROM scans WHERE state = 'running' ORDER BY started_at DESC LIMIT 1)",
                (_utcnow(),),
            )
            # The database-level singleton: two engine processes cannot both hold a running scan.
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS scans_single_running ON scans(state) WHERE state = 'running'")
            conn.execute(f"PRAGMA user_version = {int(CFG.SCHEMA_VERSION)}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        self.paths.store_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.paths.store_db, timeout=CFG.BUSY_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- scans
    def create_scan(self, requested: int, spend_cap_usd: float) -> ScanRow:
        scan_id = uuid.uuid4().hex
        now = _utcnow()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO scans(scan_id, started_at, state, requested, spend_cap_usd, heartbeat_at) "
                "VALUES (?,?,?,?,?,?)",
                (scan_id, now, "running", requested, spend_cap_usd, now),
            )
        return self.get_scan(scan_id)

    def expire_stale_scans(self, *, stale_before: str) -> int:
        """Fail 'running' scans whose heartbeat is older than ``stale_before`` (their engine exited); a read can
        call this so a dead scan never leaves a client polling it forever."""
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE scans SET state = 'failed', finished_at = ?, error = 'scan process exited before finishing', "
                f"{_RECOVER_AUTO_BATCH} WHERE state = 'running' AND heartbeat_at < ?",
                (_utcnow(), stale_before),
            )
        return cur.rowcount

    def begin_scan(
        self, requested: int, spend_cap_usd: float, *, auto_apply_policy: str, stale_before: str,
    ) -> ScanRow | None:
        """The scan lease: fail scans whose heartbeat is older than ``stale_before``, then start one unless a
        live scan holds it. One write transaction, so two engine processes cannot both win; the unique index
        is the backstop. Returns None when a live scan owns the lease."""
        scan_id = uuid.uuid4().hex
        now = _utcnow()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE scans SET state = 'failed', finished_at = ?, error = 'scan process exited before finishing', "
                f"{_RECOVER_AUTO_BATCH} WHERE state = 'running' AND heartbeat_at < ?",
                (now, stale_before),
            )
            if conn.execute("SELECT 1 FROM scans WHERE state = 'running'").fetchone() is not None:
                return None
            conn.execute(
                "INSERT INTO scans(scan_id, started_at, state, requested, spend_cap_usd, heartbeat_at, auto_apply_policy) "
                "VALUES (?,?,?,?,?,?,?)",
                (scan_id, now, "running", requested, spend_cap_usd, now, auto_apply_policy),
            )
        return self.get_scan(scan_id)

    def update_scan(self, scan_id: str, **fields: Any) -> None:
        allowed = {"finished_at", "state", "processed", "cost_usd", "error", "auto_batch_id"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"update_scan fields must be a subset of {sorted(allowed)}")
        cols = ", ".join(f"{k} = ?" for k in (*fields, "heartbeat_at"))
        with self._conn() as conn:  # every progress write is also the owning worker's proof of life
            cur = conn.execute(
                f"UPDATE scans SET {cols} WHERE scan_id = ? AND state = 'running'",  # fenced: only the lease holder
                (*fields.values(), _utcnow(), scan_id),
            )
            if cur.rowcount == 0:
                raise ScanLeaseLostError(f"scan {scan_id} no longer holds the scan lease")

    def beat_scan(self, scan_id: str) -> None:
        """Proof of life from a long model call; fenced like update_scan, so a taken-over lease raises."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE scans SET heartbeat_at = ? WHERE scan_id = ? AND state = 'running'", (_utcnow(), scan_id)
            )
            if cur.rowcount == 0:
                raise ScanLeaseLostError(f"scan {scan_id} no longer holds the scan lease")

    def set_heartbeat(self, scan_id: str, heartbeat_at: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE scans SET heartbeat_at = ? WHERE scan_id = ?", (heartbeat_at, scan_id))

    def get_scan(self, scan_id: str) -> ScanRow:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()
        if row is None:
            raise KeyError(f"scan not found: {scan_id}")
        return _scan_from_row(row)

    def list_scans(self, limit: int = 20) -> list[ScanRow]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM scans ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [_scan_from_row(r) for r in rows]

    def running_scan(self) -> ScanRow | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM scans WHERE state = 'running' ORDER BY started_at DESC").fetchone()
        return _scan_from_row(row) if row else None

    # -- proposals
    def add_verdict(self, scan_id: str, track: Track, original_artists: list[str], verdict: Verdict) -> ProposalRow:
        proposal_id = uuid.uuid4().hex
        changed = verdict.title != track.title or verdict.artist != track.artist or bool(verdict.tags)
        state = "pending" if changed and verdict.status != "unknown" else "noop"
        with self._conn() as conn:
            if conn.execute(  # first write of the transaction: proves the lease, so a fenced worker stores nothing
                "UPDATE scans SET heartbeat_at = ? WHERE scan_id = ? AND state = 'running'", (_utcnow(), scan_id)
            ).rowcount == 0:
                raise ScanLeaseLostError(f"scan {scan_id} no longer holds the scan lease")
            if verdict.status != "unknown":  # a transient failure must not discard the earlier, still usable paid result
                conn.execute(
                    "UPDATE proposals SET state = 'superseded', decided_at = ? WHERE stable_id = ? AND state = 'pending'",
                    (_utcnow(), track.id),
                )
            conn.execute(
                "INSERT INTO proposals(proposal_id, scan_id, stable_id, created_at, original_title, original_artist, "
                "original_artists_json, proposed_title, proposed_artist, original_artist_credit, tags_json, "
                "fixer_status, source, confidence, jev_kind, notes_json, error, cost_usd, state) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id, scan_id, track.id, _utcnow(), track.title, track.artist,
                    json.dumps(original_artists, ensure_ascii=False), verdict.title, verdict.artist,
                    verdict.original_artist, json.dumps(list(verdict.tags)), verdict.status, verdict.source,
                    verdict.jev_p_ok, verdict.jev_kind, json.dumps(_notes_with_producer(verdict), ensure_ascii=False),
                    verdict.error, verdict.cost_usd, state,
                ),
            )
        return self.get_proposals([proposal_id])[0]

    def get_proposals(self, proposal_ids: list[str]) -> list[ProposalRow]:
        if not proposal_ids:
            return []
        rows: list[sqlite3.Row] = []
        with self._conn() as conn:
            for chunk in chunked(proposal_ids, CFG.SQL_IN_CHUNK):  # one IN (...) must stay under the variable cap
                marks = ",".join("?" * len(chunk))
                rows.extend(conn.execute(f"SELECT * FROM proposals WHERE proposal_id IN ({marks})", chunk).fetchall())
        by_id = {r["proposal_id"]: _proposal_from_row(r) for r in rows}
        missing = [p for p in proposal_ids if p not in by_id]
        if missing:
            raise KeyError(f"proposals not found: {missing}")
        return [by_id[p] for p in proposal_ids]

    def list_proposals(self, flt: ProposalFilter) -> tuple[list[ProposalRow], int]:
        where, args = [], []
        for col, value in (("state", flt.state), ("fixer_status", flt.fixer_status), ("source", flt.source),
                           ("scan_id", flt.scan_id), ("batch_id", flt.batch_id)):
            if value is not None:
                where.append(f"{col} = ?")
                args.append(value)
        if flt.tag is not None:
            where.append("EXISTS (SELECT 1 FROM json_each(proposals.tags_json) WHERE json_each.value = ?)")
            args.append(flt.tag)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM proposals {clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM proposals {clause} ORDER BY created_at DESC, proposal_id LIMIT ? OFFSET ?",
                (*args, flt.limit, flt.offset),
            ).fetchall()
        return [_proposal_from_row(r) for r in rows], total

    def counts_by_state(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS n FROM proposals GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    def set_state(self, proposal_ids: list[str], state: ProposalState, *, batch_id: str | None = None,
                  tags_added: dict[str, list[str]] | None = None, conn: sqlite3.Connection | None = None) -> None:
        now = _utcnow()
        with self._scoped(conn) as db:
            for pid in proposal_ids:
                if tags_added is not None:
                    db.execute(
                        "UPDATE proposals SET state = ?, decided_at = ?, batch_id = ?, tags_added_json = ? "
                        "WHERE proposal_id = ?",
                        (state, now, batch_id, json.dumps(tags_added.get(pid, [])), pid),
                    )
                else:
                    db.execute(
                        "UPDATE proposals SET state = ?, decided_at = ? WHERE proposal_id = ?", (state, now, pid)
                    )

    # -- batches
    def create_batch(self, kind: Literal["manual", "auto"], policy: str, proposal_count: int) -> BatchRow:
        batch_id = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO batches(batch_id, created_at, kind, policy, proposal_count) VALUES (?,?,?,?,?)",
                (batch_id, _utcnow(), kind, policy, proposal_count),
            )
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> BatchRow:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None:
            raise KeyError(f"batch not found: {batch_id}")
        return _batch_from_row(row)

    def list_batches(self, limit: int = 20) -> list[BatchRow]:
        """The latest ``limit`` batches plus EVERY batch not yet undone, newest first: an old applied batch must stay
        discoverable (and so undoable) however many newer batches exist."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM batches WHERE undone_at IS NULL OR batch_id IN "
                "(SELECT batch_id FROM batches ORDER BY created_at DESC LIMIT ?) ORDER BY created_at DESC",
                (limit,),
            ).fetchall()
        return [_batch_from_row(r) for r in rows]

    def claim_pending(
        self, proposal_ids: list[str], batch_id: str, tags_added: dict[str, list[str]], *, fence_scan_id: str | None = None,
    ) -> list[str]:
        """Atomically move each still-pending proposal into the batch; returns the ids this call won.

        The conditional UPDATE is the claim: two concurrent applies of one proposal cannot both win it.
        ``fence_scan_id`` makes the claim also prove, in the same transaction, that this scan still holds the
        lease: a worker taken over while suspended raises ScanLeaseLostError and claims nothing.
        """
        now = _utcnow()
        claimed: list[str] = []
        with self._conn() as conn:
            if fence_scan_id is not None and conn.execute(
                "UPDATE scans SET heartbeat_at = ? WHERE scan_id = ? AND state = 'running'", (now, fence_scan_id)
            ).rowcount == 0:
                raise ScanLeaseLostError(f"scan {fence_scan_id} no longer holds the scan lease")
            for pid in proposal_ids:
                cur = conn.execute(
                    "UPDATE proposals SET state = 'applied', decided_at = ?, batch_id = ?, tags_added_json = ? "
                    "WHERE proposal_id = ? AND state = 'pending'",
                    (now, batch_id, json.dumps(tags_added.get(pid, [])), pid),
                )
                if cur.rowcount:
                    claimed.append(pid)
        return claimed

    def resolve_pending(self, proposal_ids: list[str], state: ProposalState) -> list[str]:
        """Move each still-pending proposal to ``state``; returns the ids this call won (an apply may have claimed one)."""
        now = _utcnow()
        won: list[str] = []
        with self._conn() as conn:
            for pid in proposal_ids:
                cur = conn.execute(
                    "UPDATE proposals SET state = ?, decided_at = ? WHERE proposal_id = ? AND state = 'pending'",
                    (state, now, pid),
                )
                if cur.rowcount:
                    won.append(pid)
        return won

    def reject_pending(self, proposal_ids: list[str]) -> list[str]:
        return self.resolve_pending(proposal_ids, "rejected")

    @contextmanager
    def _hold_write_lock(self, prove_ownership: Callable[[sqlite3.Connection], None]) -> Iterator[sqlite3.Connection]:
        """Take the store's write lock, prove ownership inside it, and hold it until the caller's block returns.

        A scan takeover, a batch or undo reconcile and a release all write this file, so while the lock is held
        none of them can run: the writer's ownership stays true through the state.db commit, not just up to it.
        """
        self.paths.store_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.paths.store_db, timeout=CFG.BUSY_TIMEOUT_S, isolation_level=None)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise StoreBusyError(f"the title-fix store is busy: {exc}") from exc
            prove_ownership(conn)
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            try:
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                raise FenceExitError(f"the title-fix store could not commit its fence after the write: {exc}") from exc
        finally:
            conn.close()

    def write_fence(self, batch_id: str, fence_scan_id: str | None) -> AbstractContextManager[sqlite3.Connection]:
        """The apply's fence: the batch must still be the uncommitted batch the writer claimed under, and an
        auto-apply's scan must still hold the lease; either failure raises before anything is written."""

        def prove(conn: sqlite3.Connection) -> None:
            if conn.execute(
                "SELECT 1 FROM batches WHERE batch_id = ? AND committed_at IS NULL AND undone_at IS NULL", (batch_id,)
            ).fetchone() is None:
                raise BatchReleasedError(f"batch {batch_id} is no longer open for writing")
            if fence_scan_id is not None and conn.execute(
                "UPDATE scans SET heartbeat_at = ? WHERE scan_id = ? AND state = 'running'", (_utcnow(), fence_scan_id)
            ).rowcount == 0:
                raise ScanLeaseLostError(f"scan {fence_scan_id} no longer holds the scan lease")

        return self._hold_write_lock(prove)

    def undo_fence(self, batch_id: str, token: str) -> AbstractContextManager[sqlite3.Connection]:
        """The undo's fence: the intent recorded under ``token`` must still be this writer's, not cleared and
        re-taken by an undo that judged it abandoned."""

        def prove(conn: sqlite3.Connection) -> None:
            if conn.execute(
                "SELECT 1 FROM batches WHERE batch_id = ? AND undo_started_at = ? AND undone_at IS NULL",
                (batch_id, token),
            ).fetchone() is None:
                raise UndoIntentLostError(f"the undo of batch {batch_id} no longer owns its intent")

        return self._hold_write_lock(prove)

    def exclusive(self) -> AbstractContextManager[sqlite3.Connection]:
        """The store's write lock, held for the caller's whole decision AND action. A reconcile must read the tracks
        and act on what it read under this lock, or a live writer's commit can land between the two."""
        return self._hold_write_lock(lambda _conn: None)

    @contextmanager
    def _scoped(self, conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        """The held connection when the caller owns the write lock, else a fresh one."""
        if conn is not None:
            yield conn
        else:
            with self._conn() as own:
                yield own

    def mark_batch_committed(
        self, batch_id: str, *, applied_state: dict[str, dict[str, Any]] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """The batch's track write landed: from here its rows are real changes and Undo may restore them.
        applied_state (proposal_id -> {"etag", "tags"} as of the write) lets Undo see ANY later row or tag edit."""
        state_json = None if not applied_state else json.dumps(applied_state, sort_keys=True)
        with self._scoped(conn) as db:
            db.execute(
                "UPDATE batches SET committed_at = ?, applied_state_json = ? WHERE batch_id = ?",
                (_utcnow(), state_json, batch_id),
            )

    def release_batch(self, batch_id: str, *, conn: sqlite3.Connection | None = None) -> None:
        """Return only the rows this batch claimed; another batch's rows are never touched.

        A claimed row could not be superseded by a newer scan while it was applied, so one that now has a newer
        pending proposal for its track is released as superseded: at most one pending row per track.
        """
        now = _utcnow()
        with self._scoped(conn) as db:
            db.execute(
                "UPDATE proposals SET state = 'superseded', decided_at = ?, batch_id = NULL, tags_added_json = '[]' "
                "WHERE batch_id = ? AND state = 'applied' AND EXISTS ("
                "SELECT 1 FROM proposals newer WHERE newer.stable_id = proposals.stable_id AND newer.state = 'pending')",
                (now, batch_id),
            )
            db.execute(
                "UPDATE proposals SET state = 'pending', batch_id = NULL, tags_added_json = '[]' "
                "WHERE batch_id = ? AND state = 'applied'",
                (batch_id,),
            )

    def set_batch_count(self, batch_id: str, proposal_count: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE batches SET proposal_count = ? WHERE batch_id = ?", (proposal_count, batch_id))

    def discard_batch(self, batch_id: str, *, conn: sqlite3.Connection | None = None) -> None:
        """Drop a batch whose track write failed: nothing of it landed, so no undo record may exist."""
        with self._scoped(conn) as db:
            db.execute("DELETE FROM batches WHERE batch_id = ?", (batch_id,))

    def mark_undo_started(self, batch_id: str, target_proposal_ids: list[str]) -> str | None:
        """Record the intent to undo, and exactly which proposals it restores, before any track is restored; returns
        its token, or None when another undo already holds it. The token is how the writer later proves the intent
        is still its own; the targets are how a crashed undo is later judged without counting a skipped conflict."""
        token = _utcnow() + "#" + uuid.uuid4().hex[:8]
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE batches SET undo_started_at = ?, undo_targets_json = ? WHERE batch_id = ? "
                "AND undone_at IS NULL AND undo_started_at IS NULL", (token, json.dumps(target_proposal_ids), batch_id),
            )
        return token if cur.rowcount == 1 else None

    def finish_undo(
        self, batch_id: str, *, complete: bool, token: str | None = None, conn: sqlite3.Connection | None = None,
    ) -> None:
        """Clear the intent; a complete undo also stamps the batch undone. With a ``token`` only the intent's
        owner may finish it: an undo that lost its intent changes nothing."""
        with self._scoped(conn) as db:
            db.execute(
                "UPDATE batches SET undo_started_at = NULL, undo_targets_json = NULL, undone_at = CASE WHEN ? THEN ? ELSE undone_at END "
                "WHERE batch_id = ? AND (? IS NULL OR undo_started_at = ?)", (complete, _utcnow(), batch_id, token, token),
            )

    def mark_batch_undone(self, batch_id: str) -> None:
        self.finish_undo(batch_id, complete=True)


def needs_person_selection(*, producer_credit_removed: bool, changes_artist: bool, original_member_count: int) -> bool:
    """A removed credit and a collapsed multi-member artist list each need a person's selection, whatever the policy.
    Shared with the eval scorer so its auto-write counts describe the shipped policy."""
    return producer_credit_removed or (changes_artist and original_member_count > 1)


def auto_apply_eligible(proposal: ProposalRow, policy: str) -> bool:
    """Which pending rows a policy may apply WITHOUT a human selecting them.

    review / project / not_music / unknown are never eligible: only an explicit selection applies them.
    """
    if proposal.state != "pending" or proposal.fixer_status != "fixed":
        return False
    if needs_person_selection(
        producer_credit_removed=proposal.producer_credit_removed,
        changes_artist=proposal.changes_artist,
        original_member_count=len(proposal.original_artists),
    ):
        return False
    if policy == "safe_tier":
        return proposal.source in SAFE_TIER_SOURCES
    if policy == "gate_approved":
        return True
    if policy == "off":
        return False
    raise ConfigError(f"unknown policy {policy!r}")
