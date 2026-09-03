"""Installed-machine live-stems capability assessment.

The live-stems feature must know before a performance begins whether this
machine can sustain it.  The installed engine records one real local benchmark
on its first boot, applies the named Apple-silicon thresholds below, and shares
that record with the HTTP and CLI surfaces.  A checkout intentionally has no
record: its host is a development environment, not an installed product.

Requirements:
  ✔︎ Machine capability is measured once per installed data directory.
  ✔︎ Pre-M1 hardware is refused before live stems can start.
  ✔︎ M1 and M2 select low quality, while M3 or newer selects high quality.
  ✔︎ Four decks report the eight-bar lookahead required over two decks.

Acceptance tests:
  [if] a pre-M1 machine is offered live stems [then ⛔️]
  [if] M1 selects high quality or M3 selects low quality [then ⛔️]
  [if] four decks use the two-deck lookahead [then ⛔️]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

CAPABILITY_FILE_NAME: Final[str] = "live_stems_capability.json"
CAPABILITY_SCHEMA: Final[int] = 1
LIVE_STEMS_CAPABILITY_PATH: Final[str] = "/api/v1/stems/live-capability"

MINIMUM_APPLE_SILICON_MAJOR: Final[int] = 1
HIGH_QUALITY_APPLE_SILICON_MAJOR: Final[int] = 3
BENCHMARK_ITERATIONS: Final[int] = 50_000
BENCHMARK_MIN_HASHES_PER_SECOND: Final[int] = 20_000
TWO_DECK_LOOKAHEAD_BARS: Final[int] = 4
FOUR_DECK_LOOKAHEAD_BARS: Final[int] = 8
SUPPORTED_DECK_COUNTS: Final[frozenset[int]] = frozenset({2, 4})


class LiveStemsCapabilityError(RuntimeError):
    """An install record or local benchmark cannot be honestly resolved."""


class LiveStemsMachineClass(StrEnum):
    PRE_M1 = "pre_m1"
    M1 = "m1"
    M2 = "m2"
    M3_OR_NEWER = "m3_or_newer"
    UNKNOWN = "unknown"


class LiveStemsQuality(StrEnum):
    DISABLED = "disabled"
    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True)
class LiveStemsCapability:
    """The persisted decision, including the evidence that made it."""

    schema: int
    machine_name: str
    machine_class: LiveStemsMachineClass
    benchmark_hashes_per_second: int
    enabled: bool
    quality: LiveStemsQuality
    reason: str

    def to_dict(self) -> dict[str, object]:
        """Produce JSON without leaking Enum objects into a stored record."""
        result = asdict(self)
        result["machine_class"] = self.machine_class.value
        result["quality"] = self.quality.value
        return result


@dataclass(frozen=True)
class LiveStemsPlan:
    """The per-deck-count lookahead a live-stems caller must report."""

    deck_count: int
    bpm: float
    lookahead_bars: int
    lookahead_ms: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# ----- assessment ---------------------------------------------------------
def classify_machine(machine_name: str) -> LiveStemsMachineClass:
    """Classify Apple's published chip name. Unknown never becomes capable."""
    match = re.search(r"\bApple\s+M(?P<major>\d+)\b", machine_name, re.IGNORECASE)
    if match is None:
        if "intel" in machine_name.lower():
            return LiveStemsMachineClass.PRE_M1
        return LiveStemsMachineClass.UNKNOWN
    major = int(match.group("major"))
    if major < MINIMUM_APPLE_SILICON_MAJOR:
        return LiveStemsMachineClass.PRE_M1
    if major == 1:
        return LiveStemsMachineClass.M1
    if major == 2:
        return LiveStemsMachineClass.M2
    return LiveStemsMachineClass.M3_OR_NEWER


def assess_machine(
    machine_name: str, benchmark_hashes_per_second: int
) -> LiveStemsCapability:
    """Apply the threshold table to explicit, already measured facts."""
    if not machine_name.strip():
        raise LiveStemsCapabilityError("machine name is empty; cannot classify live stems")
    if benchmark_hashes_per_second < 0:
        raise LiveStemsCapabilityError(
            "benchmark hashes per second must be non-negative, got "
            f"{benchmark_hashes_per_second}"
        )
    machine_class = classify_machine(machine_name)
    if machine_class in {
        LiveStemsMachineClass.PRE_M1,
        LiveStemsMachineClass.UNKNOWN,
    }:
        return LiveStemsCapability(
            schema=CAPABILITY_SCHEMA,
            machine_name=machine_name,
            machine_class=machine_class,
            benchmark_hashes_per_second=benchmark_hashes_per_second,
            enabled=False,
            quality=LiveStemsQuality.DISABLED,
            reason=(
                f"live stems requires Apple M{MINIMUM_APPLE_SILICON_MAJOR} or newer; "
                f"detected {machine_name} ({machine_class.value})"
            ),
        )
    if benchmark_hashes_per_second < BENCHMARK_MIN_HASHES_PER_SECOND:
        return LiveStemsCapability(
            schema=CAPABILITY_SCHEMA,
            machine_name=machine_name,
            machine_class=machine_class,
            benchmark_hashes_per_second=benchmark_hashes_per_second,
            enabled=False,
            quality=LiveStemsQuality.DISABLED,
            reason=(
                "live stems disabled because install benchmark measured "
                f"{benchmark_hashes_per_second} hashes/s, below "
                f"{BENCHMARK_MIN_HASHES_PER_SECOND} hashes/s"
            ),
        )
    quality = (
        LiveStemsQuality.HIGH
        if machine_class is LiveStemsMachineClass.M3_OR_NEWER
        else LiveStemsQuality.LOW
    )
    return LiveStemsCapability(
        schema=CAPABILITY_SCHEMA,
        machine_name=machine_name,
        machine_class=machine_class,
        benchmark_hashes_per_second=benchmark_hashes_per_second,
        enabled=True,
        quality=quality,
        reason=(
            f"{machine_name} measured {benchmark_hashes_per_second} hashes/s; "
            f"live stems enabled at {quality.value} quality"
        ),
    )


def detect_installed_machine_name() -> str:
    """Read the host's published model. This feature ships only on macOS."""
    system = platform.system()
    if system != "Darwin":
        return f"{system} {platform.machine()}"
    result = subprocess.run(
        ["sysctl", "-n", "machdep.cpu.brand_string"],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    name = result.stdout.strip()
    if result.returncode != 0 or not name:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise LiveStemsCapabilityError(
            f"could not read machdep.cpu.brand_string: {detail}"
        )
    return name


def run_install_benchmark() -> int:
    """Measure a small local CPU kernel once, returning whole hashes per second."""
    seed = b"open-dj-live-stems-install-benchmark"
    started = time.perf_counter_ns()
    digest = seed
    for _ in range(BENCHMARK_ITERATIONS):
        digest = hashlib.sha256(digest).digest()
    elapsed_ns = time.perf_counter_ns() - started
    if elapsed_ns <= 0:
        raise LiveStemsCapabilityError(
            f"benchmark clock did not advance (elapsed_ns={elapsed_ns})"
        )
    if len(digest) != 32:
        raise LiveStemsCapabilityError("SHA-256 benchmark produced an invalid digest")
    return int(BENCHMARK_ITERATIONS * 1_000_000_000 / elapsed_ns)


# ----- persistence --------------------------------------------------------
def capability_path(data_dir: Path) -> Path:
    """The state-owned record path for one installed application profile."""
    return data_dir / "state" / CAPABILITY_FILE_NAME


def persist_capability(data_dir: Path, capability: LiveStemsCapability) -> Path:
    """Atomically persist the assessment, or raise instead of losing it."""
    path = capability_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(capability.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        raise LiveStemsCapabilityError(
            f"could not persist live-stems capability at {path}: {exc}"
        ) from exc
    return path


def load_capability(data_dir: Path) -> LiveStemsCapability:
    """Load and validate the one stored decision. Corruption is a named fault."""
    path = capability_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LiveStemsCapabilityError(
            f"live-stems capability has not been assessed at {path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveStemsCapabilityError(
            f"could not read live-stems capability at {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise LiveStemsCapabilityError(f"live-stems capability at {path} is not an object")
    try:
        capability = LiveStemsCapability(
            schema=int(raw["schema"]),
            machine_name=str(raw["machine_name"]),
            machine_class=LiveStemsMachineClass(str(raw["machine_class"])),
            benchmark_hashes_per_second=int(raw["benchmark_hashes_per_second"]),
            enabled=bool(raw["enabled"]),
            quality=LiveStemsQuality(str(raw["quality"])),
            reason=str(raw["reason"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveStemsCapabilityError(
            f"live-stems capability at {path} has invalid fields: {exc}"
        ) from exc
    if capability.schema != CAPABILITY_SCHEMA:
        raise LiveStemsCapabilityError(
            f"live-stems capability at {path} has schema {capability.schema}; "
            f"expected {CAPABILITY_SCHEMA}"
        )
    return capability


def assess_install_once(data_dir: Path) -> LiveStemsCapability:
    """Reuse an assessment only when this host remains in the same class."""
    machine_name = detect_installed_machine_name()
    try:
        stored = load_capability(data_dir)
    except LiveStemsCapabilityError:
        if capability_path(data_dir).exists():
            raise
        capability = assess_machine(machine_name, run_install_benchmark())
        persist_capability(data_dir, capability)
        return capability
    if stored.machine_class is classify_machine(machine_name):
        return stored
    return reassess_persisted_capability(
        data_dir,
        machine_name=machine_name,
        benchmark_hashes_per_second=run_install_benchmark(),
    )


def reassess_persisted_capability(
    data_dir: Path,
    *,
    machine_name: str,
    benchmark_hashes_per_second: int,
) -> LiveStemsCapability:
    """Replace a copied record when it belongs to another machine class."""
    stored = load_capability(data_dir)
    if stored.machine_class is classify_machine(machine_name):
        return stored
    capability = assess_machine(machine_name, benchmark_hashes_per_second)
    persist_capability(data_dir, capability)
    return capability


# ----- live-stems plan ---------------------------------------------------
def plan_live_stems(
    capability: LiveStemsCapability, *, deck_count: int, bpm: float
) -> LiveStemsPlan:
    """Return the named per-deck-count latency budget, never an unlabelled ms."""
    if not capability.enabled:
        raise LiveStemsCapabilityError(capability.reason)
    if deck_count not in SUPPORTED_DECK_COUNTS:
        raise LiveStemsCapabilityError(
            f"live stems supports deck_count {sorted(SUPPORTED_DECK_COUNTS)}, got {deck_count}"
        )
    if not math.isfinite(bpm):
        raise LiveStemsCapabilityError(f"BPM must be finite, got {bpm}")
    if bpm <= 0:
        raise LiveStemsCapabilityError(f"BPM must be positive, got {bpm}")
    lookahead_bars = (
        TWO_DECK_LOOKAHEAD_BARS if deck_count == 2 else FOUR_DECK_LOOKAHEAD_BARS
    )
    return LiveStemsPlan(
        deck_count=deck_count,
        bpm=bpm,
        lookahead_bars=lookahead_bars,
        lookahead_ms=round(lookahead_bars * 4 * 60_000 / bpm),
    )


# ----- CLI ---------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.stems.live_capability",
        description="read the installed live-stems capability assessment",
    )
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--deck-count", type=int, choices=sorted(SUPPORTED_DECK_COUNTS))
    parser.add_argument("--bpm", type=float, default=120.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        capability = load_capability(args.data_dir)
        body: dict[str, object] = capability.to_dict()
        if args.deck_count is not None:
            body["plan"] = plan_live_stems(
                capability, deck_count=args.deck_count, bpm=args.bpm
            ).to_dict()
    except LiveStemsCapabilityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
