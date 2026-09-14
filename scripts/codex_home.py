"""Dynamic Codex-home selection for the Sol review lane.

WHY THIS EXISTS. `sol_review.py`'s local seat used to inherit whatever
CODEX_HOME the calling shell happened to have exported (or its absence,
which becomes codex's own default `~/.codex`). On Sun 14 Sep 2026 the named
`~/.codex-seat-a` credential was found expired (401 token_expired) while
`~/.codex` itself authenticated fine -- so a caller with a stale
CODEX_HOME=~/.codex-seat-a sitting in its shell sent every local-seat
review into that 401 with no explicit failure, just silence at the coverage
gate. Hardcoding the opposite (always route to ~/.codex-journey) trades one
fixed value for another and burns the standby weekend-overflow seat's quota
even when the primary seat is healthy. This module removes both problems:
probe every known home's LIVE auth, in priority order, and hand the caller
the first one that actually authenticates right now.

The probe is the SAME mechanism codex_weekly_pct_local.sh already uses --
hit chatgpt.com's usage endpoint with the bearer token from that home's
auth.json -- reused here rather than reinvented, so "does this seat work"
has exactly one implementation.

Requirements (mini-PRD):
  / The first candidate whose auth.json authenticates live is selected.
    [if a candidate is skipped despite authenticating then broken]
  / No fallback to an API key, ever -- there is none in CANDIDATE_HOMES.
    [if a caller receives an API-key path from this module then broken]
  / When no candidate authenticates, the caller gets an explicit failure,
    never a silently-chosen bad home.
    [if select_codex_home() returns instead of raising when all fail then broken]
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

#: Tried in this order. Data, not code -- reorder this tuple to reprioritize.
#: ~/.codex is this host's default seat. ~/.codex-seat-a is the named
#: "seat-a" seat (found expired Sun 14 Sep 2026). ~/.codex-journey is the
#: weekend-overflow seat the maintainer bought explicitly; it is last so a healthy
#: primary seat never routes load there and burns its separate quota.
CANDIDATE_HOMES: tuple[str, ...] = (
    str(Path.home() / ".codex"),
    str(Path.home() / ".codex-seat-a"),
    str(Path.home() / ".codex-journey"),
)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


class CodexHomeUnavailable(Exception):
    """No configured Codex home has a working auth token right now."""


def _weekly_pct(home: str, timeout_s: int = 20) -> int:
    """Live weekly-window used_percent for `home`'s auth.json.

    Raises on any failure (missing file, expired token, network, malformed
    response) -- the caller decides whether that means "try the next home"
    or "give up"; this function never guesses.
    """
    tokens = json.loads((Path(home) / "auth.json").read_text())["tokens"]
    headers = {
        "Authorization": "Bearer " + tokens["access_token"],
        "ChatGPT-Account-Id": tokens.get("account_id", ""),
        "User-Agent": "codex-cli",
    }
    request = urllib.request.Request(USAGE_URL, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload = json.load(response)
    window = (payload.get("rate_limit") or {}).get("primary_window")
    if not window:
        raise ValueError(f"{home}: no primary_window in usage response")
    return int(window["used_percent"])


def select_codex_home(candidates: tuple[str, ...] = CANDIDATE_HOMES) -> tuple[str, int]:
    """First candidate home that authenticates live, and its weekly used_percent.

    Never falls back to an API key. Raises CodexHomeUnavailable, carrying
    every candidate's failure reason, when none authenticates -- the
    caller's job is to turn that into an explicit coverage MISS, not to
    proceed with an unverified home.
    """
    failures: list[str] = []
    for home in candidates:
        try:
            pct = _weekly_pct(home)
        except (
            OSError,
            KeyError,
            ValueError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as exc:
            failures.append(f"{home}: {exc}")
            continue
        return home, pct
    raise CodexHomeUnavailable(
        f"no Codex home authenticated (tried {len(candidates)}): " + "; ".join(failures)
    )
