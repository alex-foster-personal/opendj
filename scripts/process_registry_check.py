"""Fleet process registry drift check (issue #2542).

Re-queries every host live and asserts an INVARIANT rather than a value, per
``.claude/rules/verification.md`` (prefer an invariant to a value, a
procedure to a verdict): every owned unit found live has a row in the
committed ``docs/ops/process-registry.json``, and every registry row's unit
still exists live. It never compares against a pinned count, so it does not
rot the day someone adds a new timer.

Two DRIFT shapes:

``UNREGISTERED``
    A unit matching our ownership rules exists live but has no row in the
    committed registry. Per issue #2542: "Adding a process without a
    registry row is a defect."

``STALE``
    A registry row's unit no longer exists live on its host.

And the shape this check refuses to get wrong, per the same verification
rule: an **unreachable host renders as UNKNOWN, never as clean**. Silently
treating "the host didn't answer" the same as "the host answered with zero
units" is the exact absence-of-a-bad-thing trap that rule names -- both
render identically unless the check is built to tell them apart.

    python -m scripts.process_registry_check
    python -m scripts.process_registry_check --hosts silver,nucbox-wsl

Exit codes: 0 clean, 1 drift found, 2 UNKNOWN (a host could not be reached,
or the committed registry could not be read -- never treated as a pass).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass

from scripts.process_registry_gen import HOSTS, JSON_PATH, collect_host
from scripts.process_registry_sources import Host, HostResult, scrub_identities

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_UNKNOWN = 2


@dataclass
class HostCheck:
    host: str
    status: str  # "clean" | "drift" | "unknown"
    detail: list[str]


def _load_registry() -> dict[str, list[dict]] | None:
    """host -> list of registered unit dicts, or None if unreadable."""
    if not JSON_PATH.exists():
        return None
    try:
        doc = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return {
        h["host"]: [u for u in h.get("units", []) if u.get("owned")] for h in doc.get("hosts", [])
    }


def check_host(
    host: Host,
    registry: dict[str, list[dict]],
    collector: Callable[[Host], HostResult] = collect_host,
) -> HostCheck:
    result = collector(host)
    if not result.reachable:
        # This is the negative control the check must pass: an unreachable
        # host is UNKNOWN, full stop. It is never treated as "zero owned
        # units found, therefore compliant" -- that reading is indistinguishable
        # from an actual clean host and would make an outage look like success.
        return HostCheck(host.name, "unknown", [f"host unreachable: {result.error}"])

    # The registry row was scrubbed on the way into the artifact, so the live name has
    # to be scrubbed the same way before the two are compared. Both sides call the one
    # scrubber, which is what stops a scrubbed row reading as STALE forever.
    live_owned = {scrub_identities(u.unit) for u in result.units if u.owned}
    registered = {u["unit"] for u in registry.get(host.name, [])}

    unregistered = sorted(live_owned - registered)
    stale = sorted(registered - live_owned)

    detail = [
        f"UNREGISTERED: `{unit}` runs on {host.name} but has no registry row"
        for unit in unregistered
    ]
    detail += [
        f"STALE: registry row `{unit}` on {host.name} no longer exists live" for unit in stale
    ]

    if detail:
        return HostCheck(host.name, "drift", detail)
    return HostCheck(host.name, "clean", [])


def run_check(
    hosts: list[Host],
    registry: dict[str, list[dict]] | None,
    collector: Callable[[Host], HostResult] = collect_host,
) -> tuple[int, list[HostCheck]]:
    if registry is None:
        missing = ["docs/ops/process-registry.json missing or unreadable"]
        return EXIT_UNKNOWN, [HostCheck("*", "unknown", missing)]

    checks = [check_host(h, registry, collector) for h in hosts]
    if any(c.status == "unknown" for c in checks):
        return EXIT_UNKNOWN, checks
    if any(c.status == "drift" for c in checks):
        return EXIT_DRIFT, checks
    return EXIT_OK, checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", help="comma-separated host names to check (default: all)")
    args = parser.parse_args(argv)

    hosts = HOSTS
    if args.hosts:
        wanted = set(args.hosts.split(","))
        hosts = [h for h in HOSTS if h.name in wanted]

    registry = _load_registry()
    exit_code, checks = run_check(hosts, registry)

    for c in checks:
        marker = {"clean": "OK", "drift": "DRIFT", "unknown": "UNKNOWN"}[c.status]
        print(f"[{marker}] {c.host}")
        for line in c.detail:
            print(f"    {line}")

    verdict = {EXIT_OK: "CLEAN", EXIT_DRIFT: "DRIFT FOUND", EXIT_UNKNOWN: "UNKNOWN"}[exit_code]
    print(f"\n{verdict}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
