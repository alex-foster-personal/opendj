"""AGENT-10: the mirror carries audio health DURABLY, and keeps carrying it.

WHY THIS EXISTS. On Thu 10 Sep 2026 the app went silent. The operator watched
several audio error toasts and an output-health bar on screen, and
`GET /api/v1/state/ui-mirror` published `toasts: []` with no audio health at
all. An agent reading it minutes later concluded nothing was wrong, then fell
back on `master.rms`, which taps `_masterGain` upstream of the master mute and
the destination and so reported a healthy 0.24 through a total outage.
Measured live with `master_mute` TRUE: rms 0.4687, room silent.

Two defects produced that, and both are pinned here:
  1. EPHEMERAL - the mirror mapped the LIVE toast store, and toasts dismiss in
     about 5s while the mirror publishes every 1000ms.
  2. MISSING   - `audioOutputHealth.snapshot`, the reading behind the
     output-health bar a human sees at `TopBar.svelte:605`, was mirrored
     nowhere. An AGENT-02 parity defect in its own right.

WHY IN PYTEST, when the fold already has 7 node tests. The requirement plugin
builds the coverage matrix from `@pytest.mark.requirement` markers and flags
requirements with NO tests. Frontend `.mjs` tests are invisible to it, so
AGENT-10 read as an ORPHAN requirement and nothing structurally protected it.
These are the tests that make it noticed and kept.

Regression lines:
  - if the mirror route drops or reshapes audio_health then broken
  - if ui-mirror.ts stops building an audio_health block then broken
  - if the meter is published without a staleness signal then broken (a frozen
    0.24 and a live 0.24 must not be indistinguishable, which is what cost most
    of a morning)
  - if _escalate regains a kind allowlist then broken (it can only ever DROP
    error-severity rows before they leave the browser)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes.state import router

pytestmark = pytest.mark.requirement("AGENT-10")

REPO = Path(__file__).resolve().parents[2]
RB = REPO / "apps" / "webui" / "frontend" / "src" / "lib" / "rb"
UI_MIRROR = RB / "ui-mirror.ts"
PERF_LOG = RB / "perf-event-log.ts"
HEALTH_FOLD = RB / "audio-health-mirror.ts"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_mirror_round_trips_the_audio_health_block_intact() -> None:
    """The server must not drop or reshape the block the page publishes."""
    audio_health = {
        "output": {
            "state": "running",
            "output_latency_ms": 0.0,
            "base_latency_ms": 5.8,
            "sink_id": "",
            "output_context_time_s": 1.0,
            "verdict": "dead",
        },
        "display": {"cssClass": "dead", "title": "Output-to-device: ..."},
        "meter": {"rms": 0.4687, "age_ms": 30000, "fresh": False},
        "silence_verdict": "ok",
        "recent_faults": [
            {
                "t": "2026-09-10T08:57:20.000Z",
                "kind": "silent-while-playing",
                "message": "nothing leaving the master bus",
                "age_ms": 780000,
            }
        ],
    }
    with _client() as client:
        published = client.put(
            "/api/v1/state/ui-mirror",
            json={"client_open": True, "audio_health": audio_health, "toasts": []},
        )
        got = client.get("/api/v1/state/ui-mirror")

    assert published.status_code == 202
    assert got.status_code == 200
    assert got.json()["audio_health"] == audio_health, (
        "the audio-health block must survive the round trip byte for byte; an agent "
        "diagnosing an outage reads it from here"
    )


def test_a_fault_older_than_the_toast_lifetime_survives_the_round_trip() -> None:
    """The whole point: a fault from 13 minutes ago is still readable.

    The Thu 10 Sep outage fired its errors at 08:57 and was read at 09:10.
    Toasts were long gone; only a durable record could have answered.
    """
    thirteen_minutes = 13 * 60 * 1000
    with _client() as client:
        client.put(
            "/api/v1/state/ui-mirror",
            json={
                "client_open": True,
                "toasts": [],
                "audio_health": {
                    "recent_faults": [
                        {"t": "x", "kind": "audio-output-dead", "message": "m",
                         "age_ms": thirteen_minutes}
                    ]
                },
            },
        )
        faults = client.get("/api/v1/state/ui-mirror").json()["audio_health"]["recent_faults"]

    assert faults and faults[0]["age_ms"] == thirteen_minutes, (
        "an empty toasts list must not be the only trace of an outage"
    )


def test_frontend_still_builds_an_audio_health_block() -> None:
    """Deleting the wiring must fail a test, not merely lose a feature quietly.

    Matched as a KEY, not a substring. Found by mutation: `"audio_health" in
    body` passed against a mutation renaming the key to `audio_health_REMOVED`,
    because the longer identifier still contains the shorter one. A substring
    matching a longer identifier is listed in .claude/rules/verification.md as
    a way a true positive answers the wrong question, and it made this test
    unable to detect the very removal it exists to catch.
    """
    body = UI_MIRROR.read_text()
    assert re.search(r"\baudio_health\s*:", body), (
        "ui-mirror.ts must publish `audio_health` AS A KEY; a renamed or removed "
        "key means an agent reading the mirror gets no audio health at all"
    )
    assert re.search(r"\bbuildAudioHealthMirror\s*\(", body), (
        "ui-mirror.ts must CALL the fold; a hand-rolled inline block would drift "
        "from the tested one"
    )
    assert HEALTH_FOLD.is_file(), f"{HEALTH_FOLD.name} is where the tested logic lives"


def test_the_meter_is_published_with_a_staleness_signal() -> None:
    """A frozen reading and a live one must be distinguishable.

    The RAF loop that writes the master RMS self-terminates when nothing is
    transporting, while the mirror keeps republishing the last value every
    second. Without an age, a live 0.24 and one frozen at the moment audio died
    read identically, which is exactly what happened.
    """
    fold = HEALTH_FOLD.read_text()
    assert "fresh" in fold and "age_ms" in fold
    assert "METER_FRESH_MAX_MS" in fold, "the staleness bound must be named, not inline"
    assert "rmsAgeMs !== null" in fold, (
        "an UNKNOWN meter age must not count as fresh; an absent measurement "
        "rendering as a good one is the defect class behind this whole outage"
    )


def test_escalation_has_no_kind_allowlist() -> None:
    """recordPerfEvent already gates on severity. A second filter can only DROP.

    An eight-name allowlist here silently discarded `presentation-tick-failed`,
    recorded at error with a message saying the waveform would freeze over live
    audio, and listed `presentation-stalled`, which nothing emits.
    """
    body = PERF_LOG.read_text()
    escalate = body[body.index("function _escalate(") :]
    escalate = escalate[: escalate.index("\n}\n")]
    assert "ESCALATED_KINDS" not in escalate and ".has(entry.kind)" not in escalate, (
        f"_escalate must not filter by kind:\n{escalate}"
    )
