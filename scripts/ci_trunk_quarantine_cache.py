"""Pick the Trunk quarantine list a CI run uses: live, else cached, else none.

ADR-NEW-trunk-quarantine-survives-a-budget-wall (REQ CIQUAR-02). The hosted
`trunk-quarantine-list` job fetches the list with the org token. When it ran and
returned an `ok` list, that list is used and written to CACHE_FILE so a main run
can cache it. When it did not (pull requests skip it; a budget wall stops it),
the newest list a main run cached is used if it is under MAX_AGE_HOURS old.

Fail closed: a live job that succeeded with an unusable list, no cache hit, an
unreadable file, an epoch in the future, a list that is not `ok`, or one over the
age cap all publish source=none and no list, so the verdict script excuses nothing.
The live list is trusted only when the fetch succeeded, and the cache is
consulted only when it did not.

Environment: LIVE_LIST, LIVE_RESULT (the fetch step's outcome, else the job's
result), CACHE_HIT ("true"/"false"), CACHE_FILE,
MAX_AGE_HOURS, GITHUB_OUTPUT. Pure stdlib, so the runner's own python3 runs it.

    python3 -m scripts.ci_trunk_quarantine_cache
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

NOTICE_UNDER_HOURS = 24
LIVE_RESULTS = ("success", "failure", "cancelled", "skipped")


@dataclass(frozen=True)
class Resolution:
    source: str  # live | cached | none
    tests: str | None  # compact JSON of the list, or None
    level: str  # "" (plain log line) | notice | warning
    message: str


def _usable(doc: object) -> bool:
    return isinstance(doc, dict) and doc.get("status") == "ok" and isinstance(doc.get("tests"), list)


def _parse(raw: str) -> object:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def resolve(
    *,
    live_list: str,
    live_result: str,
    cache_hit: bool,
    cache_file: Path,
    max_age_hours: int,
    now: int,
) -> Resolution:
    """Decide the list; writes cache_file only when the live list is used."""
    # Only a fetch that succeeded can supply the list: the fetch step is
    # continue-on-error, so a step that wrote an output and then failed must not.
    live = _parse(live_list) if live_list and live_result == "success" else None
    if _usable(live):
        compact = json.dumps(live, separators=(",", ":"))
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        at = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
        cache_file.write_text(
            json.dumps({"fetched_at": at, "fetched_epoch": now, "list": live}, separators=(",", ":")),
            encoding="utf-8",
            newline="\n",
        )
        count = len(live["tests"])  # type: ignore[index]
        return Resolution("live", compact, "", f"Quarantine list: live from Trunk, {count} tests")

    why = f"the live list job ended {live_result or 'unknown'}"
    # A job that succeeded yet returned no usable list is a defect, not an outage:
    # fail closed rather than let an older list excuse tests.
    if live_result == "success":
        return Resolution(
            "none", None, "warning",
            "the live list job succeeded but its list is unusable, so nothing is quarantined",
        )
    # Shared self-hosted runners keep their workspace between jobs, so a file
    # this job did not restore is refused, not read.
    if not cache_hit or not cache_file.is_file() or cache_file.stat().st_size == 0:
        return Resolution(
            "none", None, "warning",
            f"no live list ({why}) and no cached list, so nothing is quarantined",
        )
    try:
        doc = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    epoch = doc.get("fetched_epoch") if isinstance(doc, dict) else None
    cached = doc.get("list") if isinstance(doc, dict) else None
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch > now or not _usable(cached):
        return Resolution(
            "none", None, "warning",
            f"the cached list is unreadable or dated in the future, so nothing is quarantined ({why})",
        )
    # The epoch is the only authority: fetched_at is display text a stale or
    # hand-edited file could disagree with, so the time shown comes from the epoch.
    at = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    age_h = (now - epoch) // 3600
    if age_h >= max_age_hours:
        return Resolution(
            "none", None, "warning",
            f"the cached list from {at} is {age_h}h old, over the {max_age_hours}h cap, "
            f"so nothing is quarantined ({why})",
        )
    level = "notice" if live_result == "skipped" and age_h < NOTICE_UNDER_HOURS else "warning"
    count = len(cached["tests"])  # type: ignore[index]
    return Resolution(
        "cached", json.dumps(cached, separators=(",", ":")), level,
        f"using the list cached at {at} ({age_h}h old, {count} tests) because {why}",
    )


def main() -> int:
    env = os.environ
    output = env.get("GITHUB_OUTPUT")
    if not output:
        print("::error title=Trunk quarantine::GITHUB_OUTPUT is not set", file=sys.stderr)
        return 2
    try:
        max_age = int(env["MAX_AGE_HOURS"])
        cache_file = Path(env["CACHE_FILE"])
    except (KeyError, ValueError) as exc:
        print(f"::error title=Trunk quarantine::bad configuration: {exc}", file=sys.stderr)
        return 2
    live_result = env.get("LIVE_RESULT", "")
    cache_hit = env.get("CACHE_HIT", "")
    # A typo or a missing wire must not read as "skipped" or "no hit".
    if live_result not in LIVE_RESULTS or cache_hit not in ("true", "false"):
        print(
            "::error title=Trunk quarantine::bad configuration: "
            f"LIVE_RESULT={live_result!r} CACHE_HIT={cache_hit!r}",
            file=sys.stderr,
        )
        return 2
    res = resolve(
        live_list=env.get("LIVE_LIST", ""),
        live_result=live_result,
        cache_hit=cache_hit == "true",
        cache_file=cache_file,
        max_age_hours=max_age,
        now=int(time.time()),
    )
    print(f"::{res.level} title=Trunk quarantine::{res.message}" if res.level else res.message)
    with open(output, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"source={res.source}\n")
        if res.tests is not None:
            fh.write(f"list={res.tests}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
