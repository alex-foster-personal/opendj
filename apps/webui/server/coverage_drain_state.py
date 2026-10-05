"""The coverage drain's persisted setting and its status record.

Split out of ``coverage_drain`` (which sits near the file-size ceiling). The
drain re-exports both, so callers keep importing them from there.

The setting lives in ``<data_dir>/state/coverage-drain.json``:

    {"enabled": true,
     "steps": {"vocals": true, "lyrics": true, "analysis": true},
     "transient_bundle_cap": 2}

Every key is optional and an absent key is its documented default (all on,
cap 2). An unknown key or a wrong type raises: a typo must not silently turn
a step back on.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 HEALTH-10 per-step switches
    [if] the file is absent [then] the drain and every step are on
    [if] ``steps.analysis`` is false [then] only analysis is off
    [if] a step name is unknown or a value is not a bool [then ⛔️] raise
    [if] one key is updated [then] the others keep their stored value
    [if] two updates overlap [then] both keys land and neither raises
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from apps.webui.server.coverage_cloud_vocals import DEFAULT_TRANSIENT_CAP, MAX_TRANSIENT_CAP

CONFIG_FILENAME: str = "coverage-drain.json"
#: Steps a user can switch off one by one. Matches the drain's job order.
SWITCHABLE_STEPS: tuple[str, ...] = ("vocals", "lyrics", "analysis")
CONFIG_KEYS: frozenset[str] = frozenset({"enabled", "steps", "transient_bundle_cap"})
#: One load-merge-write of the setting at a time, so overlapping PUTs cannot
#: drop each other's keys or race on a temp file.
_UPDATE_LOCK = threading.Lock()


def config_path(data_dir: Path) -> Path:
    return data_dir / "state" / CONFIG_FILENAME


class DrainConfig:
    """The persisted setting. Absent file or key = the documented default."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"{self.path} must hold a JSON object")
        unknown = set(payload) - CONFIG_KEYS
        if unknown:
            raise ValueError(f"{self.path}: unknown key(s) {sorted(unknown)}")
        return payload

    def enabled(self) -> bool:
        value = self._load().get("enabled", True)
        if not isinstance(value, bool):
            raise TypeError(f"{self.path}: 'enabled' must be true or false, got {value!r}")
        return value

    def steps(self) -> dict[str, bool]:
        stored = self._load().get("steps", {})
        if not isinstance(stored, dict):
            raise TypeError(f"{self.path}: 'steps' must be an object, got {stored!r}")
        return {**dict.fromkeys(SWITCHABLE_STEPS, True), **_validated_steps(stored, self.path)}

    def transient_bundle_cap(self) -> int:
        return _validated_cap(
            self._load().get("transient_bundle_cap", DEFAULT_TRANSIENT_CAP), self.path
        )

    def update(
        self,
        *,
        enabled: bool | None = None,
        steps: dict[str, bool] | None = None,
        transient_bundle_cap: int | None = None,
    ) -> None:
        """Change the named keys; every other stored key keeps its value."""
        with _UPDATE_LOCK:
            payload = self._load()
            if enabled is not None:
                payload["enabled"] = enabled
            if steps is not None:
                payload["steps"] = {**self.steps(), **_validated_steps(steps, self.path)}
            if transient_bundle_cap is not None:
                payload["transient_bundle_cap"] = _validated_cap(transient_bundle_cap, self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload) + "\n")
                os.replace(temporary, self.path)
            except BaseException:
                if os.path.exists(temporary):
                    os.unlink(temporary)
                raise

    def set_enabled(self, enabled: bool) -> None:
        self.update(enabled=enabled)


def _validated_steps(steps: dict[str, Any], where: Path) -> dict[str, bool]:
    for step, value in steps.items():
        if step not in SWITCHABLE_STEPS:
            raise ValueError(f"{where}: unknown step {step!r}; expected one of {SWITCHABLE_STEPS}")
        if not isinstance(value, bool):
            raise TypeError(f"{where}: steps.{step} must be true or false, got {value!r}")
    return dict(steps)


def _validated_cap(value: Any, where: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TRANSIENT_CAP:
        raise ValueError(
            f"{where}: transient_bundle_cap must be a whole number 0..{MAX_TRANSIENT_CAP}, "
            f"got {value!r}"
        )
    return value


@dataclass
class DrainStatus:
    #: idle | green | running | blocked | paused_playing | yielding_user_jobs
    #: | yielding_memory_pressure | stopped | disabled
    state: str = "idle"
    enabled: bool = True
    #: Per-step switches (``DrainConfig.steps``); a step that is off never runs.
    steps_enabled: dict[str, bool] = field(default_factory=dict)
    stop_requested: bool = False
    #: Plain-language cause of a ``blocked`` state; None otherwise.
    reason: str | None = None
    pending: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    waiting_on_stems: int = 0
    #: Truly unrendered: no local bundle and none in the R2 index.
    stems_needing_farm: list[str] = field(default_factory=list)
    stems_needing_farm_count: int = 0
    #: Present tracks whose bundle is evicted and fetchable from R2 (done).
    stems_in_cloud: int = 0
    #: Present tracks recorded as having no stems to make (finished).
    stems_no_source: int = 0
    #: ok | off | unknown. While unknown, a missing bundle is neither "in
    #: cloud" nor "needs the farm": ``stems_unclassified`` counts those.
    stems_index_state: str = "off"
    stems_index_reason: str | None = None
    stems_unclassified: int = 0
    #: Vocals whose bundle must be fetched from R2 first, the transient
    #: bundles held, the cap, bytes fetched, and why fetching is paused.
    cloud_vocals: dict[str, Any] = field(default_factory=dict)
    #: The terminal-stems check: tracks checked and marked this process.
    stems_check: dict[str, Any] = field(default_factory=dict)
    #: Why analysis is being held back by memory pressure; None when it is not.
    memory_pressure: str | None = None
    #: Steps this install cannot run at all, with the reason.
    unavailable_steps: dict[str, str] = field(default_factory=dict)
    #: Analysis stages reported and never run here (they need torch), with
    #: the reason, and how many present tracks have no current record.
    farm_only_stages: dict[str, str] = field(default_factory=dict)
    farm_only_pending: dict[str, int] = field(default_factory=dict)
    next_retry_at: float | None = None
    last_job: dict[str, Any] | None = None
    jobs_run: int = 0
    jobs_failed: int = 0
    ticks: int = 0
    updated_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "CONFIG_FILENAME",
    "SWITCHABLE_STEPS",
    "DrainConfig",
    "DrainStatus",
    "config_path",
]
