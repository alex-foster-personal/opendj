"""Machine performance tier - minimal gate for feature scalers (PERFMODE-01 stub).

Reads host RAM once. A first-run canary benchmark belongs here later; until
then RAM is the honest signal local stems needs (~1.2 GiB peak per part-1).

Agent-native parity: ``python -m apps.shared.perf_tier`` prints the tier.
"""

from __future__ import annotations

import json
from enum import StrEnum


class PerfTier(StrEnum):
    LOW = "LOW"
    STANDARD = "STANDARD"
    HIGH = "HIGH"


# Local stems needs headroom for torch + source decode (part-1 ~1.2 GiB RSS).
LOCAL_STEMS_MIN_TIER: PerfTier = PerfTier.STANDARD

_TIER_ORDER: tuple[PerfTier, ...] = (PerfTier.LOW, PerfTier.STANDARD, PerfTier.HIGH)


def _tier_rank(tier: PerfTier) -> int:
    return _TIER_ORDER.index(tier)


def detect_tier() -> PerfTier:
    """Derive tier from installed RAM. Fail loud if psutil is missing."""
    import psutil

    ram_gb = psutil.virtual_memory().total / (1024**3)
    if ram_gb >= 16.0:
        return PerfTier.HIGH
    if ram_gb >= 8.0:
        return PerfTier.STANDARD
    return PerfTier.LOW


def meets_floor(required: PerfTier, actual: PerfTier | None = None) -> bool:
    tier = actual if actual is not None else detect_tier()
    return _tier_rank(tier) >= _tier_rank(required)


def local_stems_tier_refusal(actual: PerfTier | None = None) -> str | None:
    """Why local stems are inert on this machine, or None when allowed."""
    tier = actual if actual is not None else detect_tier()
    if meets_floor(LOCAL_STEMS_MIN_TIER, tier):
        return None
    return (
        f"local stems require at least {LOCAL_STEMS_MIN_TIER.value} machine tier "
        f"(detected {tier.value})"
    )


def tier_wire(tier: PerfTier | None = None) -> dict[str, str]:
    resolved = tier if tier is not None else detect_tier()
    return {"tier": resolved.value, "min_local_stems_tier": LOCAL_STEMS_MIN_TIER.value}


def main(argv: list[str] | None = None) -> int:
    _ = argv
    print(json.dumps(tier_wire(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
