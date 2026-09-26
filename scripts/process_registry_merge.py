"""Merge freshly-collected process-registry snapshots with the previous one.

Split out of ``process_registry_gen.py`` (issue #2542, PR #3827) once this
concern grew a real docstring worth keeping: a scoped ``--hosts`` regen must
carry forward what it did NOT just query, WITHOUT discarding what was
previously known. That is a genuinely separate question from "how do we
query a host" (``process_registry_gen.py`` collectors) or "how do we render
the snapshot" (``process_registry_gen.render_markdown``), so it lives here
rather than being cut for line count alone.

Supersedes: `_load_previous` and `merge_with_previous` in
`scripts/process_registry_gen.py`; moved here and deleted there.
`carry_forward_unqueried_host` is new in PR #3827.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.process_registry_sources import HostResult

# ------------------------------------------------------------ previous snapshot


def _load_previous(json_path: Path) -> dict[str, dict]:
    """Map host -> previous host block from the last committed snapshot."""
    if not json_path.exists():
        return {}
    try:
        doc = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {h["host"]: h for h in doc.get("hosts", [])}


def carry_forward_unqueried_host(prev_block: dict) -> dict:
    """A --hosts filter narrower than the full fleet: carry an untouched host
    forward rather than silently dropping it, WITHOUT discarding its real
    reachable/error/stale_as_of (found reviewing PR #3827's own scoped
    regen, which replaced four hosts' real ssh/DNS errors with a generic
    "not queried" string, and flipped a fifth (agentbox) from
    reachable=True with real data to reachable=False with
    stale_as_of=None -- "unreachable and we don't even know when it last
    worked", strictly less informative than either true state, and exactly
    the absence-of-a-bad-thing trap .claude/rules/verification.md warns
    about). A host not queried this pass is neither confirmed reachable nor
    newly unreachable -- it is unmeasured, so its last REAL measurement
    must survive, tagged as not re-verified rather than replaced.

    The real last-known error lives in its OWN field, ``last_known_error``,
    never folded into ``error`` itself (claude-review, PR #3827, round 4,
    P3): ``error`` stays the constant ``not_queried_note`` across any
    number of consecutive scoped regens, so it can never grow a nested
    "last known: not queried this pass -- last known: ..." prefix the way
    re-wrapping ``error`` on every pass would.
    """
    not_queried_note = "not queried this pass (--hosts filter)"
    if prev_block.get("reachable"):
        stale_as_of = prev_block.get("generated_at_utc_of_block")
        last_known_error = None
    else:
        stale_as_of = prev_block.get("stale_as_of") or prev_block.get(
            "generated_at_utc_of_block"
        )
        # Carry the ORIGINAL real error forward, not this pass's rendering
        # of it: if prev_block was itself already a carried block, its real
        # error is in last_known_error, not error (which is just the
        # constant note by then). A previously-reachable host carried twice
        # has last_known_error=None -- that None is the real state (found
        # by claude-review, PR #3827, round 6, P3), so it must NOT then
        # fall back to prev_block["error"], which by that point is only
        # this same not_queried_note, and folding it into last_known_error
        # would nest a non-measurement into a diagnosis.
        if prev_block.get("error") == not_queried_note:
            last_known_error = prev_block.get("last_known_error")
        else:
            last_known_error = prev_block.get("last_known_error") or prev_block.get("error")
    return {
        **prev_block,
        "reachable": False,
        "error": not_queried_note,
        "last_known_error": last_known_error,
        "stale_as_of": stale_as_of,
    }


def merge_with_previous(results: list[HostResult], previous: dict[str, dict]) -> list[dict]:
    """Fill in a stale-but-labeled block for any host unreachable THIS pass.

    An unreachable host must never render as "zero owned units" -- that is
    indistinguishable from a host that is genuinely clean, which is the
    absence-of-a-bad-thing trap this whole design exists to avoid. Instead
    it keeps the last known-good snapshot for that host, explicitly marked
    stale with the timestamp it actually came from.
    """
    merged: list[dict] = []
    for r in results:
        if r.reachable:
            merged.append(
                {
                    "host": r.host,
                    "reachable": True,
                    "error": None,
                    "stale_as_of": None,
                    "units": [u.to_json() for u in r.units],
                }
            )
            continue
        prev = previous.get(r.host)
        merged.append(
            {
                "host": r.host,
                "reachable": False,
                "error": r.error,
                "stale_as_of": prev.get("generated_at_utc_of_block") if prev else None,
                "units": prev["units"] if prev else [],
            }
        )
    return merged

