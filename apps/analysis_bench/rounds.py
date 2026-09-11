"""The round log: the counter that lets the next session resume, not guess.

A measurement nobody can place in a sequence is not an experiment, it is an
anecdote. Each `run` that scores a real bundle appends one numbered block to
its lane's experiment log naming the scorer version, the bundle identity and
the host, so a later reader can tell a rescoring apart from a re-measurement
and can see which arm moved.

WHY A ROUND WITHOUT CONTROLS IS REFUSED RATHER THAN FLAGGED. Spec section 6
requires a positive and a negative control in every table, and beatgrid round 0
is the reason: it published F-measures between 0.30 and 0.86 with no floor, so
none of them had an interpretation until round 1 measured that a rigid 128 BPM
grid which never opens the audio scores 0.280. A warning would have been
ignored; the refusal is what makes the floor non-optional.

WHY THE NUMBER IS SCANNED AND NOT STORED. Two workers on two branches would
both read a stored counter and both write round N. Reading the log itself means
the number is a fact about the document a reviewer is looking at.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

# The one greppable shape. `scan` and `append` must never disagree about it, so
# both go through these two constants.
HEADING = "### {lane} bench round {number}"
_HEADING_RE = re.compile(r"^###\s+(?P<lane>[a-z_]+)\s+bench\s+round\s+(?P<number>\d+)\s*$", re.M)


class RoundError(RuntimeError):
    """A round could not be logged. Never downgraded to a warning."""


def next_round_number(text: str, lane: str, *, floor: int) -> int:
    """One past the highest round this lane has posted here, never below its floor."""
    posted = [
        int(match.group("number"))
        for match in _HEADING_RE.finditer(text)
        if match.group("lane") == lane
    ]
    return max([floor, *[n + 1 for n in posted]])


def _utc_day() -> str:
    """`Wed 9 Sep 2026`, built without the POSIX-only unpadded-day directive.

    Microsoft's C runtime rejects that directive, so `run --post` raised instead
    of appending on a Windows-native run (Codex P2 BLOCKING, PR #1582). The day
    number is taken from the struct_time field instead. The weekday stays
    because it is the checksum on the number.
    """
    now = time.gmtime()
    return f"{time.strftime('%a', now)} {now.tm_mday} {time.strftime('%b %Y', now)}"


def render_round(lane: str, number: int, report: dict[str, Any], *, host: str) -> str:
    """The markdown block for one round, or a refusal when it cannot be read."""
    arms = report.get("arms") or {}
    roles = {arm.get("role") for arm in arms.values()}
    missing = [role for role in ("positive_control", "negative_control") if role not in roles]
    if missing:
        raise RoundError(
            f"{lane} round {number} carries no {' and no '.join(missing)}. "
            "Spec section 6 requires both in every table: without a floor and a "
            "ceiling the candidate's figures have no interpretation. Re-run with "
            "the lane's controls, or do not post the round."
        )

    bundle = report.get("bundle") or {}
    named = ", ".join(
        f"{name} ({arm.get('role', 'candidate')})" for name, arm in sorted(arms.items())
    )
    lines = [
        HEADING.format(lane=lane, number=number),
        "",
        f"Measured {_utc_day()} on host `{host}` with "
        f"scorer {report['scorer_version']}, bundle {bundle.get('lane')}/{bundle.get('version')} "
        f"(bundle_id `{bundle.get('bundle_id')}`).",
        "",
        f"Arms: {named}.",
        "",
        report.get("table", "").rstrip(),
        "",
    ]
    return "\n".join(lines)


def append_round(
    log_path: Path, lane: str, report: dict[str, Any], *, floor: int, host: str
) -> int:
    """Append the next numbered round for `lane`, returning the number written."""
    log_path = Path(log_path)
    if not log_path.exists():
        raise RoundError(
            f"{log_path} does not exist. A round appended to a file nobody reads is "
            "a round nobody can resume; create the lane's experiment log first."
        )
    text = log_path.read_text(encoding="utf-8")
    number = next_round_number(text, lane, floor=floor)
    block = render_round(lane, number, report, host=host)
    separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    log_path.write_text(text + separator + block, encoding="utf-8")
    return number
