"""Machine performance tier - LOW/STANDARD/HIGH for feature scalers (PERFMODE-01).

Engine-side psutil host read plus optional user override. Agent-native parity:
``python -m apps.shared.perf_tier`` / ``python -m apps.engine_core.perf_tier``.
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Literal, NamedTuple

RAM_HIGH_BYTES: int = 16 * 1024**3
RAM_STANDARD_BYTES: int = 8 * 1024**3
CPU_HIGH: int = 8
CPU_STANDARD: int = 4

MiB: int = 1024 * 1024

SCALERS: dict[str, dict[str, int | str]] = {
    "LOW": {
        "prefetch_tracks": 2,
        "prefetch_bytes": 24 * MiB,
        "preview_pcm_bytes": 64 * MiB,
        "anlz_entries": 8,
        "anlz_bytes": 10 * MiB,
        "stem_waveform_entries": 12,
        "stem_waveform_bytes": 128 * MiB,
        "stem_decode": "mix-only",
        "worker_divisor": 2,
    },
    "STANDARD": {
        "prefetch_tracks": 4,
        "prefetch_bytes": 48 * MiB,
        "preview_pcm_bytes": 160 * MiB,
        "anlz_entries": 32,
        "anlz_bytes": 40 * MiB,
        "stem_waveform_entries": 24,
        "stem_waveform_bytes": 256 * MiB,
        "stem_decode": "mix-first",
        "worker_divisor": 1,
    },
    "HIGH": {
        "prefetch_tracks": 6,
        "prefetch_bytes": 96 * MiB,
        "preview_pcm_bytes": 256 * MiB,
        "anlz_entries": 64,
        "anlz_bytes": 80 * MiB,
        "stem_waveform_entries": 36,
        "stem_waveform_bytes": 512 * MiB,
        "stem_decode": "eager",
        "worker_divisor": 1,
    },
}


class PerfTier(StrEnum):
    LOW = "LOW"
    STANDARD = "STANDARD"
    HIGH = "HIGH"


class HostFacts(NamedTuple):
    logical_cpus: int
    ram_bytes: int


class HostInfoUnavailable(RuntimeError):
    """Host facts could not be read."""


class InvalidPerfTierOverride(ValueError):
    """User override string is not auto|low|standard|high."""


class CanaryThresholdsUnmeasured(RuntimeError):
    """Canary clamp floors are not measured yet."""


# Local stems needs headroom for torch + source decode (part-1 ~1.2 GiB RSS).
LOCAL_STEMS_MIN_TIER: PerfTier = PerfTier.STANDARD

_TIER_ORDER: tuple[PerfTier, ...] = (PerfTier.LOW, PerfTier.STANDARD, PerfTier.HIGH)
_VALID_OVERRIDES: frozenset[str] = frozenset({"auto", "low", "standard", "high"})


def _tier_rank(tier: PerfTier) -> int:
    return _TIER_ORDER.index(tier)


def classify_auto(facts: HostFacts) -> PerfTier:
    if facts.ram_bytes >= RAM_HIGH_BYTES and facts.logical_cpus >= CPU_HIGH:
        return PerfTier.HIGH
    if facts.ram_bytes >= RAM_STANDARD_BYTES and facts.logical_cpus >= CPU_STANDARD:
        return PerfTier.STANDARD
    return PerfTier.LOW


def read_host_facts() -> HostFacts:
    """Measured (logical_cpus, ram_bytes) via psutil. Raises HostInfoUnavailable.

    Moved here from ``apps.engine_core.host_info`` (issue: trunk quality
    ratchet, arch.contracts_broken) so this module has one direction of
    dependency -- callers reach into ``apps.shared`` for host facts, not the
    other way around. ``apps.engine_core.host_info.read_host_facts`` now
    re-exports this function rather than defining its own and calling back
    into here, which used to make ``engine_core`` and ``shared`` a package
    cycle.
    """
    import psutil

    try:
        logical_cpus = psutil.cpu_count(logical=True)
    except Exception as exc:
        raise HostInfoUnavailable(f"psutil.cpu_count failed: {exc}") from exc
    if logical_cpus is None:
        raise HostInfoUnavailable("psutil.cpu_count returned None")
    try:
        ram_bytes = psutil.virtual_memory().total
    except Exception as exc:
        raise HostInfoUnavailable(f"psutil.virtual_memory failed: {exc}") from exc
    if not isinstance(ram_bytes, int) or ram_bytes <= 0:
        raise HostInfoUnavailable(f"psutil.virtual_memory().total invalid: {ram_bytes!r}")
    return HostFacts(logical_cpus=logical_cpus, ram_bytes=ram_bytes)


def detect_tier() -> PerfTier:
    """Derive auto tier from live host facts."""
    return classify_auto(read_host_facts())


def parse_override(raw: str | None) -> PerfTier | None:
    """Return None for auto; a PerfTier for explicit override; raise if invalid."""
    if raw is None:
        return None
    value = raw.strip()
    if value == "auto":
        return None
    if value not in ("low", "standard", "high"):
        raise InvalidPerfTierOverride(
            f"perf_tier must be auto|low|standard|high, got {raw!r}"
        )
    return PerfTier(value.upper())


def resolve_tier(
    *,
    facts: HostFacts | None = None,
    override: str | None = None,
    data_dir: Path | None = None,
) -> PerfTier:
    from apps.shared.paths import DATA_DIR

    pref_override = (
        override
        if override is not None
        else read_override_from_prefs(data_dir if data_dir is not None else DATA_DIR)
    )
    explicit = parse_override(pref_override)
    if explicit is not None:
        return explicit
    host = facts if facts is not None else read_host_facts()
    return classify_auto(host)


def scalers_for(tier: PerfTier) -> dict[str, int | str]:
    return dict(SCALERS[tier.value])


def stem_decode_eagerness(tier: PerfTier) -> Literal["mix-only", "mix-first", "eager"]:
    value = scalers_for(tier)["stem_decode"]
    if value == "mix-only":
        return "mix-only"
    if value == "mix-first":
        return "mix-first"
    return "eager"


def background_worker_count(default: int, tier: PerfTier | None = None) -> int:
    resolved = tier if tier is not None else resolve_tier()
    divisor = int(scalers_for(resolved)["worker_divisor"])
    return max(1, default // divisor)


def classify_from_canary(_hashes_per_second: int) -> PerfTier:
    raise CanaryThresholdsUnmeasured(
        "canary clamp thresholds are not measured; perf-tier-canary.json is "
        "recorded for evidence only"
    )


def read_override_from_prefs(data_dir: Path) -> str:
    path = data_dir / "state" / "ui-prefs.json"
    if not path.is_file():
        return "auto"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "auto"
    if not isinstance(raw, dict):
        return "auto"
    value = raw.get("perf_tier", "auto")
    if not isinstance(value, str):
        return "auto"
    lowered = value.strip().lower()
    return lowered if lowered in _VALID_OVERRIDES else "auto"


def meets_floor(required: PerfTier, actual: PerfTier | None = None) -> bool:
    tier = actual if actual is not None else resolve_tier()
    return _tier_rank(tier) >= _tier_rank(required)


def local_stems_tier_refusal(actual: PerfTier | None = None) -> str | None:
    """Why local stems are inert on this machine, or None when allowed."""
    tier = actual if actual is not None else resolve_tier()
    if meets_floor(LOCAL_STEMS_MIN_TIER, tier):
        return None
    return (
        f"local stems require at least {LOCAL_STEMS_MIN_TIER.value} machine tier "
        f"(detected {tier.value})"
    )


def tier_wire(
    data_dir: Path | None = None,
    *,
    facts: HostFacts | None = None,
    host_failure: str | None = None,
    override: str | None = None,
) -> dict[str, object]:
    from apps.shared.paths import DATA_DIR

    data = data_dir if data_dir is not None else DATA_DIR
    pref_override = read_override_from_prefs(data) if override is None else override
    host: HostFacts | None = facts
    if host is None and host_failure is None:
        try:
            host = read_host_facts()
        except HostInfoUnavailable as exc:
            host_failure = str(exc)

    auto_tier: PerfTier | None = None
    if host is not None:
        auto_tier = classify_auto(host)
    elif pref_override == "auto":
        raise HostInfoUnavailable(host_failure or "host facts unavailable")

    explicit = parse_override(pref_override)
    if explicit is not None:
        resolved = explicit
        source = "override"
    else:
        assert auto_tier is not None
        resolved = auto_tier
        source = "auto"

    body: dict[str, object] = {
        "tier": resolved.value,
        "source": source,
        "auto_tier": auto_tier.value if auto_tier is not None else None,
        "override": pref_override,
        "min_local_stems_tier": LOCAL_STEMS_MIN_TIER.value,
        "scalers": scalers_for(resolved),
    }
    if host is not None:
        body["host"] = {
            "logical_cpus": host.logical_cpus,
            "ram_bytes": host.ram_bytes,
        }
    return body


def main(argv: list[str] | None = None) -> int:
    _ = argv
    try:
        wire = tier_wire()
    except HostInfoUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(wire, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
