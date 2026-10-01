"""Per-step drain switches and the memory pressure gate (HEALTH-10).

Regression lines:
  - if a switched-off step ever runs then broken
  - if switching one step off stops another then broken
  - if a switched-off analysis step keeps the drain from reporting green then broken
  - if updating one config key resets another then broken
  - if a typo in a step name is accepted then broken
  - if analysis starts while memory pressure is high then broken
  - if memory pressure stops vocals or lyrics then broken
  - if swap that is merely allocated holds analysis back then broken

[if] a switched-off step runs or memory pressure fails to hold analysis [then] fail, [else stop].
"""
from __future__ import annotations

import json

import pytest

from apps.webui import coverage_drain_cli as cli
from apps.webui.server import coverage_drain as cd
from apps.webui.server import coverage_memory as mem
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library
from tests.health_lights.test_coverage_drain import LRC, Lrclib
from tests.health_lights.test_coverage_drain_analysis import Rig, _covered_except_analysis

pytestmark = pytest.mark.requirement("HEALTH-10")

CONFIG = "/api/v1/coverage-drain/config"


def _needs_everything(library: Library, stable_id: str) -> None:
    audio = fx.audio_file(library.music, f"{stable_id}.mp3")
    fx.seed_track(library.state_db, stable_id, str(audio))
    fx.write_stem_bundle(library.stems, stable_id)


# --- the setting -----------------------------------------------------------------


def test_defaults_are_all_on_and_absent_keys_stay_default(tmp_path) -> None:
    config = cd.DrainConfig(cd.config_path(tmp_path))
    assert config.enabled() is True
    assert config.steps() == {"vocals": True, "lyrics": True, "analysis": True}
    assert config.transient_bundle_cap() == 2


def test_updating_one_key_keeps_the_others(tmp_path) -> None:
    config = cd.DrainConfig(cd.config_path(tmp_path))
    config.update(steps={"analysis": False})
    config.update(enabled=False)
    config.update(transient_bundle_cap=5)
    config.update(steps={"lyrics": False})

    assert config.enabled() is False
    assert config.steps() == {"vocals": True, "lyrics": False, "analysis": False}
    assert config.transient_bundle_cap() == 5
    config.update(steps={"analysis": True})
    assert config.steps() == {"vocals": True, "lyrics": False, "analysis": True}


@pytest.mark.parametrize(
    "payload",
    [
        {"steps": {"analysys": False}},
        {"steps": {"stems": False}},
        {"steps": {"analysis": "off"}},
        {"transient_bundle_cap": -1},
        {"transient_bundle_cap": 17},
        {"transient_bundle_cap": True},
    ],
)
def test_a_typo_or_wrong_type_is_refused_not_ignored(tmp_path, payload: dict) -> None:
    config = cd.DrainConfig(cd.config_path(tmp_path))
    with pytest.raises((TypeError, ValueError)):
        config.update(**payload)
    assert not config.path.exists()                 # nothing was written


def test_a_stored_unknown_key_raises_on_read(tmp_path) -> None:
    path = cd.config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"enabled": True, "analysis": False}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown key"):
        cd.DrainConfig(path).steps()


# --- the switches in the drain ------------------------------------------------------


def test_analysis_off_runs_vocals_and_lyrics_then_reports_green(library: Library) -> None:
    _needs_everything(library, "a")
    rig = Rig(library, lrclib=Lrclib({"a": LRC}))
    rig.drain.update_config(steps={"analysis": False})

    states = [rig.drain.tick() for _ in range(4)]

    assert states == ["ran:vocals", "ran:lyrics", "green", "green"]
    assert rig.analysis_runs == []
    assert rig.drain.status().steps_enabled == {"vocals": True, "lyrics": True, "analysis": False}
    # Control: switched back on, the very same track IS analyzed.
    rig.drain.update_config(steps={"analysis": True})
    assert rig.drain.tick() == "ran:analysis"
    assert rig.analysis_runs == ["a"]


def test_vocals_off_still_runs_lyrics_and_analysis_and_says_why_it_is_blocked(
    library: Library,
) -> None:
    _needs_everything(library, "a")
    rig = Rig(library, lrclib=Lrclib({"a": LRC}))
    rig.drain.update_config(steps={"vocals": False})

    states = [rig.drain.tick() for _ in range(3)]

    assert states == ["ran:lyrics", "ran:analysis", "blocked"]
    assert rig.vocals_runs == []
    assert "vocals is switched off in the drain setting" in str(rig.drain.status().reason)


def test_switches_over_http_and_cli(library: Library) -> None:
    _covered_except_analysis(library, "a")
    rig = Rig(library)
    library.app.state.coverage_drain = rig.drain
    client = library.client

    off = client.put(CONFIG, json={"steps": {"analysis": False}})
    assert off.status_code == 200, off.text
    assert off.json()["steps_enabled"] == {"vocals": True, "lyrics": True, "analysis": False}
    assert off.json()["enabled"] is True            # untouched
    assert rig.drain.tick() == "green" and rig.analysis_runs == []

    assert client.put(CONFIG, json={"steps": {"analysys": False}}).status_code == 422
    assert client.put(CONFIG, json={}).status_code == 422
    assert client.put(CONFIG, json={"stepz": {}}).status_code == 422
    capped = client.put(CONFIG, json={"transient_bundle_cap": 4})
    assert capped.status_code == 200 and capped.json()["steps_enabled"]["analysis"] is False

    assert cli.request_for("step-off", step="analysis") == (
        "PUT", CONFIG, {"steps": {"analysis": False}},
    )
    assert cli.request_for("step-on", step="vocals") == ("PUT", CONFIG, {"steps": {"vocals": True}})
    assert cli.request_for("transient-cap", value=4) == ("PUT", CONFIG, {"transient_bundle_cap": 4})
    assert cli.request_for("status") == ("GET", "/api/v1/coverage-drain/status", None)
    with pytest.raises(ValueError, match="--step"):
        cli.request_for("step-off")


# --- memory pressure ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "available_gib", "held"),
    [
        (1, 8.0, False),
        (2, 8.0, True),          # kernel says warn: hold, whatever is "available"
        (4, 8.0, True),
        (1, 3.9, True),          # kernel calm but under two jobs' worth of room
        (1, 4.0, False),
        (None, 8.0, False),      # not macOS / unreadable: available memory decides
        (None, 3.0, True),
    ],
)
def test_pressure_signal_and_thresholds(level, available_gib: float, held: bool) -> None:
    reason = mem.pressure_reason(lambda: level, lambda: int(available_gib * mem.GIB))
    assert (reason is not None) is held


def test_the_real_signal_reads_on_this_machine() -> None:
    """The instrument fires: a real level (macOS) or None, and real bytes."""
    level = mem.macos_pressure_level()
    assert level is None or level in mem.MACOS_PRESSURE_NAMES
    assert mem.available_bytes() > 0
    reason = mem.pressure_reason()
    assert reason is None or "memory" in reason


def test_analysis_waits_under_memory_pressure_and_resumes_after(library: Library) -> None:
    _covered_except_analysis(library, "a")
    rig = Rig(library)
    pressure: list[str | None] = ["the kernel reports memory pressure warn (level 2)"]
    rig.drain.analysis_policy = cd.AnalysisPolicy(
        user_jobs_fn=lambda: False, recency_fn=list,
        memory_pressure_fn=lambda: pressure[0],
    )

    assert rig.drain.tick() == "yielding_memory_pressure"
    assert rig.analysis_runs == []
    status = rig.drain.status()
    assert status.memory_pressure == pressure[0]
    assert "analysis waits: the kernel reports memory pressure warn" in str(status.reason)

    pressure[0] = None
    assert rig.drain.tick() == "ran:analysis"
    assert rig.drain.status().memory_pressure is None


def test_memory_pressure_does_not_stop_vocals_or_lyrics(library: Library) -> None:
    _needs_everything(library, "a")
    rig = Rig(library, lrclib=Lrclib({"a": LRC}))
    rig.drain.analysis_policy = cd.AnalysisPolicy(
        user_jobs_fn=lambda: False, recency_fn=list,
        memory_pressure_fn=lambda: "8 GiB swap in use",
    )

    states = [rig.drain.tick() for _ in range(3)]

    assert states == ["ran:vocals", "ran:lyrics", "yielding_memory_pressure"]
    assert rig.analysis_runs == []
