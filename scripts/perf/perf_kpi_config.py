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
    machine: str
    tracks: tuple[TrackProfile, ...]


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


def load_config() -> PerfKpiConfig:
    """Resolve CFG from env. Air install sets the three stable_id env vars."""
    tracks = (
        TrackProfile(
            key="small_mp3",
            stable_id=_require_stable_id(
                "MDT_PERF_KPI_SMALL_STABLE_ID",
                "67f6957f7a3b4c2d9e1f0a8b6c5d4e3f2a1b0c9d",
            ),
            label="small MP3 4.1 MB / 126 s (#1501 harness)",
        ),
        TrackProfile(
            key="large_mp3",
            stable_id=_require_stable_id(
                "MDT_PERF_KPI_LARGE_STABLE_ID",
                "3fa8b00fa2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7b",
            ),
            label="large MP3 32.4 MB / 1008 s (#1501 harness)",
        ),
        TrackProfile(
            key="stemmed_mp3",
            stable_id=_require_stable_id(
                "MDT_PERF_KPI_STEMMED_STABLE_ID",
                "8682ab12cd34ef56a7890bcde1234567890abcd",
            ),
            label="stemmed MP3 20.3 MB / 631 s (#1501 harness)",
        ),
    )
    state_dir = Path(_env("MDT_PERF_KPI_STATE_DIR") or str(DEFAULT_STATE_DIR))
    return PerfKpiConfig(
        ledger_path=Path(_env("MDT_PERF_KPI_LEDGER") or str(DEFAULT_LEDGER)),
        state_dir=state_dir,
        health_log=state_dir / "health.jsonl",
        history_log=state_dir / "history.jsonl",
        health_state=state_dir / "health-state.json",
        preview_health_url=(
            _env("MDT_PERF_KPI_PREVIEW_HEALTH_URL") or DEFAULT_PREVIEW_HEALTH_URL
        ),
        preview_engine_label=(
            _env("MDT_PERF_KPI_PREVIEW_ENGINE_LABEL") or DEFAULT_PREVIEW_ENGINE_LABEL
        ),
        scratch_port=int(_env("MDT_PERF_KPI_SCRATCH_PORT") or DEFAULT_SCRATCH_PORT),
        samples=int(_env("MDT_PERF_KPI_SAMPLES") or DEFAULT_SAMPLES),
        machine=_env("MDT_PERF_KPI_MACHINE") or "air",
        tracks=tracks,
    )


def kpi_name(leg: str, profile_key: str) -> str:
    return f"deck_load_{leg}_warm_median_ms_{profile_key}"
