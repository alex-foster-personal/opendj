"""Vocals for tracks whose stem bundle is evicted to R2: fetch one, derive, let go.

Vocal regions are derived from a stem bundle. After eviction the bundle is in
R2, not on disk, so the coverage drain has to fetch it first. Fetching every
missing bundle at once would refill the very disk the evictor just emptied,
and the evictor would answer by removing the user's least recently used
bundles: two correct features thrashing each other. So:

* ONE bundle per drain job: hydrate, derive the vocal-cache entry, done.
* The bundle is then an ordinary cached bundle, subject to normal eviction.
  This module remembers which bundles it fetched (``transient``) and never
  holds more than ``transient_bundle_cap`` of them: before fetching another
  it removes its own oldest, and only when R2 still holds that bundle byte
  for byte and no deck has it open. A bundle a deck adopted is forgotten,
  not removed.
* It does not fetch at all while the stem cache has no room: free disk must
  exceed the evictor's floor by :data:`HEADROOM_BYTES_PER_BUNDLE` per
  transient slot, so a fetch can never push the disk under the floor and
  trigger an eviction.
* A track whose vocals already exist is never fetched for. The fixed point
  is therefore "every in-cloud track has vocals": after that the drain asks
  for nothing, whatever the evictor removes.

A hub or network failure pauses cloud vocals for :data:`PAUSE_S` and spends
no attempt on the track: it is about the connection, not the audio.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 HEALTH-08 bounded transient bundles
    [if] the drain has fetched ``cap`` bundles [then] the oldest is released
      before the next fetch, so at most ``cap`` are ever on disk
    [if] a transient bundle is open on a deck [then ⛔️] it is never removed
    [if] R2 does not hold the bundle byte for byte [then ⛔️] never removed
  ✔︎ ✅ 🎯 HEALTH-08 never fights the evictor
    [if] free disk is under floor + headroom [then] no fetch, reason reported
    [if] no hydration source is armed [then] no fetch, reason reported
  ✔︎ ✅ 🎯 HEALTH-08 fixed point
    [if] vocals exist for a track [then ⛔️] its bundle is not fetched again
    [if] the evictor removes every bundle between jobs [then] each bundle is
      still downloaded exactly once
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.cloud import stem_bundles, stem_cache_budget, stem_hydration
from apps.cloud.stem_source import StemHydrationSource
from apps.vocals import cache as vocals_cache

log = logging.getLogger(__name__)

TRANSIENT_FILENAME: str = "coverage-transient-bundles.json"
TRANSIENT_SCHEMA: int = 1
DEFAULT_TRANSIENT_CAP: int = 2
MAX_TRANSIENT_CAP: int = 16
#: Room one transient bundle may need above the evictor's floor. The preview
#: library's bundles average 34 MB (27.9 GB over 828); the largest are under
#: 200 MB.
HEADROOM_BYTES_PER_BUNDLE: int = 256 * 1024 * 1024
#: How long cloud vocals wait after a hub or network failure.
PAUSE_S: float = 300.0

JobFn = Callable[[str, str], None]


class CloudPaused(Exception):
    """The connection, not the track, failed. No attempt is spent."""


@dataclass(frozen=True)
class CloudInputs:
    """What the engine contributes to one fetch decision, read at call time."""

    stems_dir: Path
    index: stem_cache_budget.StemAssetIndex
    protected: frozenset[str]
    source: StemHydrationSource | None


#-----------------------------------------------------------------------------
# the transient ledger
#-----------------------------------------------------------------------------
class TransientStore:
    """Bundles this drain fetched and has not yet let go, oldest first."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != TRANSIENT_SCHEMA:
            raise ValueError(f"{self.path} is not a schema {TRANSIENT_SCHEMA} transient ledger")
        return list(payload["bundles"])

    def save(self, bundles: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps({"schema": TRANSIENT_SCHEMA, "bundles": bundles}, indent=1) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def transient_path(data_dir: Path) -> Path:
    return data_dir / "state" / TRANSIENT_FILENAME


#-----------------------------------------------------------------------------
# the runner
#-----------------------------------------------------------------------------
@dataclass
class CloudVocals:
    data_dir: Path
    inputs_fn: Callable[[], CloudInputs]
    cap_fn: Callable[[], int]
    #: The ordinary from-local-stems vocals job, run once the bundle is here.
    derive: JobFn
    disk_fn: Callable[[Path], stem_cache_budget.DiskUsage] = stem_cache_budget.measure_disk
    clock: Callable[[], float] = time.time
    bundles_downloaded: int = 0
    bytes_downloaded: int = 0
    paused_until: float = 0.0
    paused_reason: str | None = None
    _store: TransientStore = field(init=False)

    def __post_init__(self) -> None:
        self._store = TransientStore(transient_path(self.data_dir))

    # --- gate ---------------------------------------------------------------
    def refusal(self) -> str | None:
        """Why no bundle may be fetched right now, or None when one may."""
        cap = self.cap_fn()
        if cap <= 0:
            return "transient_bundle_cap is 0: cloud vocals are switched off"
        if self.clock() < self.paused_until:
            return f"paused after a hub failure: {self.paused_reason}"
        inputs = self.inputs_fn()
        if inputs.source is None:
            return "no stem hydration source is armed on this machine"
        settings = stem_cache_budget.load_settings(self.data_dir)
        disk = self.disk_fn(inputs.stems_dir)
        cache = sum(b.size_bytes for b in stem_cache_budget.scan_bundles(inputs.stems_dir))
        room = stem_cache_budget.derived_budget_bytes(cache, disk, settings) - cache
        needed = cap * HEADROOM_BYTES_PER_BUNDLE
        if room < needed:
            return (
                f"the stem cache has {room} bytes of room above its free-disk floor and "
                f"cloud vocals need {needed}; fetching now would force an eviction"
            )
        return None

    # --- transient bookkeeping ----------------------------------------------
    def held(self, inputs: CloudInputs | None = None) -> list[dict[str, Any]]:
        """Ledger entries whose bundle is still on disk (others were evicted)."""
        stems_dir = (inputs or self.inputs_fn()).stems_dir
        return [e for e in self._store.load() if (stems_dir / str(e["stable_id"])).is_dir()]

    def _release_down_to(self, keep: int, inputs: CloudInputs) -> None:
        held = self.held(inputs)
        while len(held) > keep:
            entry = held.pop(0)
            stable_id = str(entry["stable_id"])
            if stable_id in inputs.protected:
                continue    # a deck adopted it: no longer ours to remove
            bundle = next(
                (b for b in stem_cache_budget.scan_bundles(inputs.stems_dir)
                 if b.stable_id == stable_id),
                None,
            )
            if bundle is None:
                continue
            gap = stem_cache_budget.unconfirmed_reason(bundle, inputs.index)
            if gap == stem_bundles.REASON_GONE:
                continue    # an eviction pass removed it first
            if gap is not None:
                log.warning("cloud vocals: keeping %s, R2 does not confirm it (%s)", stable_id, gap)
                continue
            # The eviction passes can claim this bundle concurrently; the
            # rename claim lets exactly one remove it, the loser sees False.
            stem_bundles.claim_and_remove(bundle.path)
        self._store.save(held)

    # --- one job -------------------------------------------------------------
    def run(self, stable_id: str, audio_path: str) -> None:
        cache_entry = vocals_cache.cache_path(self.data_dir, stable_id)
        if vocals_cache.load_valid_entry(cache_entry, Path(audio_path)) is not None:
            return    # vocals exist: never fetch a bundle for them again
        inputs = self.inputs_fn()
        if inputs.source is None:
            raise CloudPaused("no stem hydration source is armed on this machine")
        self._release_down_to(self.cap_fn() - 1, inputs)
        outcome = stem_hydration.hydrate_one(
            stable_id,
            data_dir=self.data_dir,
            source=inputs.source,
            index={sid: dict(files) for sid, files in inputs.index.items()},
            stems_dir=inputs.stems_dir,
        )
        if outcome.status == "hub_error":
            self.paused_until = self.clock() + PAUSE_S
            self.paused_reason = outcome.reason
            raise CloudPaused(outcome.reason or "hub transport failure")
        if outcome.status == "hydrated":
            self._store.save(
                [*self.held(inputs),
                 {"stable_id": stable_id, "bytes": outcome.bytes_fetched, "at": self.clock()}]
            )
            self.bundles_downloaded += 1
            self.bytes_downloaded += outcome.bytes_fetched
        elif outcome.status != "already_local":
            raise RuntimeError(f"stem bundle fetch {outcome.status}: {outcome.reason}")
        self.derive(stable_id, audio_path)

    def status(self) -> dict[str, Any]:
        return {
            "transient_held": len(self.held()),
            "transient_bundle_cap": self.cap_fn(),
            "bundles_downloaded": self.bundles_downloaded,
            "bytes_downloaded": self.bytes_downloaded,
        }


__all__ = [
    "DEFAULT_TRANSIENT_CAP",
    "HEADROOM_BYTES_PER_BUNDLE",
    "MAX_TRANSIENT_CAP",
    "PAUSE_S",
    "TRANSIENT_FILENAME",
    "CloudInputs",
    "CloudPaused",
    "CloudVocals",
    "TransientStore",
    "transient_path",
]
