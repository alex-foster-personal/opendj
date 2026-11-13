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
   beats the environment key; the build-provided key is out of scope.
   [if] a user key is saved then key_status().source == "user" and the hint shows 4 chars [else ⛔️]
   [if] neither user nor env key exists then key_status().source == "none" [else ⛔️]
"""

from __future__ import annotations

from ._helpers import chunked
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
from .title_fix_store_config import (
    KeyStatus,
    StorePaths,
    TitleFixConfig,
    clear_user_key,
    load_config,
    resolve_key,
    save_config,
    save_user_key,
)
from .title_fix_store_db import ProposalStore
from .title_fix_store_types import (
    CFG,
    AutoApplyPolicy,
    POLICIES,
    ProposalState,
    SAFE_TIER_SOURCES,
    ScanState,
    BatchReleasedError,
    ConfigError,
    CONTEXT_TAGS,
    FenceExitError,
    IdentifyItem,
    ScanLeaseLostError,
    StoreBusyError,
    UndoIntentLostError,
    VERSION_FAMILY_TAGS,
    identify_notes,
    tag_family,
)


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
