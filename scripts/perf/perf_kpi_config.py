"""Pinned stable_ids and paths for the perf KPI job unit (issue #1506)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = REPO_ROOT / "docs" / "perf" / "kpi-ledger.json"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "af-perf-kpi"
DEFAULT_HEALTH_LOG = DEFAULT_STATE_DIR / "health.jsonl"
DEFAULT_HISTORY_LOG = DEFAULT_STATE_DIR / "history.jsonl"
DEFAULT_HEALTH_STATE = DEFAULT_STATE_DIR / "health-state.json"
DEFAULT_PREVIEW_HEALTH_URL = "http://127.0.0.1:8728/api/v1/health"
DEFAULT_PREVIEW_ENGINE_LABEL = "com.af.opendj-preview-engine"
DEFAULT_SCRATCH_PORT = 8699
DEFAULT_SAMPLES = 5
CEILING_FACTOR = 3.0
CEILING_WINDOW_DAYS = 7
LEDGER_PR_TITLE = "perf(kpi): nightly ledger"
LEDGER_PR_BRANCH = "perf/kpi-nightly-ledger"


@dataclass(frozen=True)
class TrackProfile:
    key: str
    stable_id: str
    label: str


@dataclass(frozen=True)
class PerfKpiConfig:
    ledger_path: Path
    state_dir: Path
    health_log: Path
    history_log: Path
    health_state: Path
    preview_health_url: str
    preview_engine_label: str
    scratch_port: int
    samples: int
    machine: str  #: may be "" -- required only on the nightly path, see require_machine_label()
    tracks: tuple[TrackProfile, ...]
    #: Supersedes: REPO_ROOT's own checkout, which update_ledger_pr used to
    #: `git checkout -B` into on main; publishing now happens only here.
    ledger_worktree: Path
    data_dir: Path | None = None


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _require_stable_id(env_name: str, fallback: str | None) -> str:
    value = _env(env_name) or fallback
    if not value or value.startswith("REPLACE_"):
        raise ValueError(f"{env_name} must name a real library stable_id")
    return value


def require_machine_label(config: PerfKpiConfig) -> str:
    """No hidden "air" default (claude-review, PR #3827, rounds 3-4,
    P1/BLOCKING): a plist that omits MDT_PERF_KPI_MACHINE -- because the
    installer wasn't re-run after this fix, or because someone invokes
    perf_kpi_job.py directly outside either installed launchd agent -- must
    fail loudly rather than silently attribute a second Mac's KPI entries
    to Air in the shared ledger.

    Called only from the NIGHTLY path (round 4, P1/BLOCKING): the health
    tick never attributes a ledger entry to a machine, but Air's
    already-installed `com.af.perf-kpi-health` plist predates
    `MDT_PERF_KPI_MACHINE` entirely (it was never in that template's
    ``EnvironmentVariables``). Requiring it in `load_config()` itself --
    which BOTH `cmd_health` and `cmd_nightly` call -- would have broken
    Air's every-10-minutes wedged-engine detection immediately on merge,
    with no reinstall step shipped alongside this change to fix it.
    """
    if not config.machine:
        raise ValueError(
            "MDT_PERF_KPI_MACHINE must be set for the nightly path (the "
            "installed nightly plist always sets it once --host-label is "
            "passed to install_perf_kpi_launchd.sh; a manual invocation "
            "must export it explicitly). The health path does not require "
            "it, since a health tick never attributes a ledger entry."
        )
    return config.machine


def load_config() -> PerfKpiConfig:
    """Resolve CFG from env. Air install sets the three stable_id env vars."""
    tracks = (
        TrackProfile(
            key="small_mp3",
            stable_id=_require_stable_id(
                "MDT_PERF_KPI_SMALL_STABLE_ID",
                "6739d7d3865ccc8204e265c0157369666841acfd",
            ),
            label="small MP3 4.1 MB / 126 s (Air-verified)",
        ),
        TrackProfile(
            key="large_mp3",
            stable_id=_require_stable_id(
                "MDT_PERF_KPI_LARGE_STABLE_ID",
                "a918118117aac4c01c1b660a0add558640add0d2",
            ),
            label="large MP3 32.4 MB / 1008 s (Air-verified)",
        ),
        TrackProfile(
            key="stemmed_mp3",
            stable_id=_require_stable_id(
                "MDT_PERF_KPI_STEMMED_STABLE_ID",
                "a76d51f6fa000ec9b7048ad51703de2cdf1d52a8",
            ),
            label="stemmed MP3 27 MB / 598 s (Air-verified; /stems 200)",
        ),
    )
    state_dir = Path(_env("MDT_PERF_KPI_STATE_DIR") or str(DEFAULT_STATE_DIR))
    raw_data_dir = _env("MDT_PERF_KPI_DATA_DIR")
    data_dir = Path(raw_data_dir).expanduser().resolve() if raw_data_dir else None
    return PerfKpiConfig(
        ledger_path=Path(_env("MDT_PERF_KPI_LEDGER") or str(DEFAULT_LEDGER)),
        state_dir=state_dir,
        health_log=state_dir / "health.jsonl",
        history_log=state_dir / "history.jsonl",
        health_state=state_dir / "health-state.json",
        preview_health_url=(_env("MDT_PERF_KPI_PREVIEW_HEALTH_URL") or DEFAULT_PREVIEW_HEALTH_URL),
        preview_engine_label=(
            _env("MDT_PERF_KPI_PREVIEW_ENGINE_LABEL") or DEFAULT_PREVIEW_ENGINE_LABEL
        ),
        scratch_port=int(_env("MDT_PERF_KPI_SCRATCH_PORT") or DEFAULT_SCRATCH_PORT),
        samples=int(_env("MDT_PERF_KPI_SAMPLES") or DEFAULT_SAMPLES),
        machine=_env("MDT_PERF_KPI_MACHINE") or "",
        tracks=tracks,
        ledger_worktree=state_dir / "ledger-worktree",
        data_dir=data_dir,
    )


def kpi_name(leg: str, profile_key: str) -> str:
    return f"deck_load_{leg}_warm_median_ms_{profile_key}"
