"""Footprint map validation and runner-minute budget for the e2e spec selector."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scripts.ci_e2e_selection_constants import (
    BUDGET_NAME,
    FullSuite,
    GATES,
    HEX40,
    MAX_MAP_AGE_DAYS,
    STATIC_GATES,
    Git,
)


@dataclass(frozen=True)
class Footprints:
    sha: str
    built: datetime
    paths: tuple[str, ...]
    ignored: frozenset[str]
    specs: dict[str, dict]
    gates: dict[str, dict]
    source: str

    def index(self, path: str) -> int | None:
        try:
            return self.paths.index(path)
        except ValueError:
            return None

    def seconds(self, spec: str) -> float:
        return float(self.specs.get(spec, {}).get("seconds", 0.0))

    def gate_seconds(self, gate: str) -> float:
        return float(self.gates.get(gate, {}).get("seconds", 0.0))


def _check_entry(label: str, name: str, entry: object, paths: list[str]) -> None:
    if not isinstance(entry, dict) or entry.get("status") not in {"passed", "skipped", "unmeasured"}:
        raise FullSuite(f"{label}: {name} has no status of passed, skipped or unmeasured")
    files = entry.get("files")
    if not isinstance(files, list) or any(not isinstance(i, int) or not 0 <= i < len(paths) for i in files):
        raise FullSuite(f"{label}: {name} has a file index outside paths")
    if not isinstance(entry.get("seconds", 0.0), (int, float)):
        raise FullSuite(f"{label}: {name} has a non-numeric seconds")


def validate_map(raw: object, label: str) -> Footprints:
    """A map that can be selected over, or FullSuite naming what is wrong with it."""
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise FullSuite(f"{label}: not a schema 1 footprint map")
    sha, built, paths = raw.get("sha"), raw.get("built"), raw.get("paths")
    specs, ignored, gates = raw.get("specs"), raw.get("ignored", []), raw.get("gates", {})
    if not isinstance(sha, str) or not HEX40.match(sha):
        raise FullSuite(f"{label}: sha {sha!r} is not a 40-hex commit")
    try:
        built_at = datetime.fromisoformat(str(built))
    except ValueError as exc:
        raise FullSuite(f"{label}: built {built!r} is not an ISO-8601 timestamp") from exc
    if built_at.tzinfo is None:
        raise FullSuite(f"{label}: built {built!r} carries no timezone")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise FullSuite(f"{label}: paths is not a list of strings")
    if not isinstance(specs, dict) or not specs:
        raise FullSuite(f"{label}: specs is not a non-empty object")
    if not isinstance(ignored, list) or not all(isinstance(p, str) for p in ignored):
        raise FullSuite(f"{label}: ignored is not a list of strings")
    if not isinstance(gates, dict):
        raise FullSuite(f"{label}: gates is not an object")
    for spec, entry in specs.items():
        _check_entry(label, f"spec {spec}", entry, paths)
    for gate, entry in gates.items():
        _check_entry(label, f"gate {gate}", entry, paths)
        if gate not in GATES or not isinstance(entry.get("specs"), list):
            raise FullSuite(f"{label}: gate {gate} is not a footprinted gate with a specs list")
    return Footprints(sha, built_at, tuple(paths), frozenset(ignored), specs, gates, label)


def load_map(published: Path | None, seed: Path) -> Footprints:
    """The published map when it exists and validates, else the committed seed."""
    if published is not None and published.exists():
        return validate_map(json.loads(published.read_text(encoding="utf-8")), f"published map {published}")
    if not seed.exists():
        raise FullSuite(f"no footprint map: {seed} is missing and nothing was published")
    return validate_map(json.loads(seed.read_text(encoding="utf-8")), f"committed seed {seed}")


def check_fresh(footprints: Footprints, now: datetime, head: str, git: Git) -> None:
    age_days = (now - footprints.built).total_seconds() / 86400
    if age_days > MAX_MAP_AGE_DAYS:
        raise FullSuite(
            f"the footprint map ({footprints.source}, {footprints.sha[:9]}) is {age_days:.1f} days old, "
            f"over the {MAX_MAP_AGE_DAYS}-day limit; refresh it (scripts/e2e_footprints_refresh.py)"
        )
    try:
        git(["merge-base", "--is-ancestor", footprints.sha, head])
    except subprocess.CalledProcessError as exc:
        raise FullSuite(
            f"the footprint map's sha {footprints.sha[:9]} is not an ancestor of the merge commit {head}; "
            "the map was built on another line of history"
        ) from exc


@dataclass(frozen=True)
class BudgetConfig:
    minutes: dict[str, float]
    root_overhead_min: float
    gate_overhead_min: float
    min_per_recorded_minute: float
    #: Estimated minutes per static gate (no footprint, no recorded seconds), so an unmeasured
    #: gate is never free to the budget.
    static_gates_min: dict[str, float]

    def for_context(self, queue_draft: bool) -> float:
        return self.minutes["queue_draft" if queue_draft else "pr_head"]


def load_budget(path: Path) -> BudgetConfig:
    """`ci/e2e-budget.json`; missing or malformed is FullSuite, never a silent "no budget"."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as missing:
        raise FullSuite(f"the e2e budget `{path}` is missing") from missing
    except ValueError as bad:
        raise FullSuite(f"the e2e budget `{path}` is not valid JSON: {bad}") from bad
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise FullSuite(f"the e2e budget `{path}` must be an object with `version: 1`")
    try:
        model = raw["model"]
        config = BudgetConfig(
            minutes={context: float(raw["runner_minutes"][context]) for context in ("pr_head", "queue_draft")},
            root_overhead_min=float(model["root_overhead_min"]),
            gate_overhead_min=float(model["gate_overhead_min"]),
            min_per_recorded_minute=float(model["min_per_recorded_minute"]),
            static_gates_min={gate: float(raw["static_gates_min"][gate]) for gate in STATIC_GATES},
        )
    except (KeyError, TypeError, ValueError) as bad:
        raise FullSuite(f"the e2e budget `{path}` is missing or mistypes a field: {bad!r}") from bad
    if any(m <= 0 for m in config.minutes.values()):
        raise FullSuite(f"the e2e budget `{path}`: runner minutes must be positive")
    return config
