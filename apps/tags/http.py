"""Delivery-layer seam of ``apps.tags`` for the title fixer (ADR-NEW-module-contracts).

The title fixer's web routes, its service and its evaluation script live outside this package (the routes
and service in ``apps.webui.server``, the script in ``scripts``), and the module-surface rule lets a delivery
layer or composition root reach a package only through ``apps.tags.contract`` or this module. This module is
that single seam: it re-exports exactly the names those three callers use, so nothing outside the package deep
imports ``title_fix`` or ``title_fix_store``.

It is an interim seam, not the target shape. The target is the dedup/reconcile shape: the router and service
move into ``apps.tags`` behind a Pydantic contract. That needs the service's state-backend dependencies behind a
contract first, so it is a follow-up (``(private path)``), and the list below only shrinks.
"""

from __future__ import annotations

from . import title_fix
from .title_fix import CFG as FIXER_CFG
from .title_fix import (
    Track,
    Verdict,
    fix_tracks,
    project_rules,
    project_tags,
    regex_propose,
    safe_tier,
    verdict_row,
    with_tags,
)
from .title_fix_store import CFG as STORE_CFG
from .title_fix_store import (
    BatchReleasedError,
    BatchRow,
    ConfigError,
    FenceExitError,
    KeyStatus,
    ProposalFilter,
    ProposalRow,
    ProposalStore,
    ScanLeaseLostError,
    ScanRow,
    StoreBusyError,
    StorePaths,
    TitleFixConfig,
    UndoIntentLostError,
    auto_apply_eligible,
    chunked,
    clear_user_key,
    load_config,
    needs_person_selection,
    resolve_key,
    save_config,
    save_user_key,
    tag_family,
)

__all__ = [
    "FIXER_CFG",
    "STORE_CFG",
    "BatchReleasedError",
    "BatchRow",
    "ConfigError",
    "FenceExitError",
    "KeyStatus",
    "ProposalFilter",
    "ProposalRow",
    "ProposalStore",
    "ScanLeaseLostError",
    "ScanRow",
    "StoreBusyError",
    "StorePaths",
    "TitleFixConfig",
    "Track",
    "UndoIntentLostError",
    "Verdict",
    "auto_apply_eligible",
    "chunked",
    "clear_user_key",
    "fix_tracks",
    "load_config",
    "needs_person_selection",
    "project_rules",
    "project_tags",
    "regex_propose",
    "resolve_key",
    "safe_tier",
    "save_config",
    "save_user_key",
    "tag_family",
    "title_fix",
    "verdict_row",
    "with_tags",
]
