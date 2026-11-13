"""CFG, shared exceptions, tag-family helpers and identify row shapes (split from title_fix_store)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .identify import IdentifyProposal
from .title_fix import VERSION_TAGS


class CFG:
    SCHEMA_VERSION = (
        4  # PRAGMA user_version once the schema and column migrations are applied; bump with a new migration
    )
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


@dataclass(frozen=True)
class IdentifyItem:
    stable_id: str
    original_title: str
    original_artist: str
    original_artists: list[str]
    proposal: IdentifyProposal


def identify_notes(proposal: IdentifyProposal) -> list[str]:
    best = proposal.best
    notes = [
        f"musicbrainz recording: {best.recording_id}",
        f"acoustid score: {proposal.score:.2f}",
    ]
    if best.year is not None:
        notes.append(f"year: {best.year}")
    if best.label:
        notes.append(f"label: {best.label}")
    if proposal.status == "needs_review":
        for cand in proposal.candidates:
            year_bit = str(cand.year) if cand.year is not None else "?"
            notes.append(f"candidate: {cand.artist} - {cand.title} ({year_bit}) {cand.recording_id}")
    return notes


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
CONTEXT_TAGS = frozenset(
    {
        "soundtrack",
        "unreleased",
        "mik-analyzed",
        "low-bitrate",
        "streaming-only",
        "youtube-rip",
        "beatport-sourced",
        "duplicate-copy",
        "video-source",
        "live-set",
        "dj-mix",
    }
)


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
