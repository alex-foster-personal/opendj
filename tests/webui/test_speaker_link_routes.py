"""AUDIO-DEVICE-03..05: acoustic proof overlay, input listing and speaker reconnect routes."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.acoustic_proof import ACOUSTIC_PROOF_TTL_S, AcousticProof, overlay_acoustic_proof
from apps.webui.server.routes import audio_output_health as routes

_SHELL_OK: dict[str, Any] = {
    "device_delivering": True,
    "verdict": "ok",
    "reason": None,
    "default_device_name": "HYPERBOOM",
    "default_device_uid": "10-94-97-1C-C6-43:output",
    "io_cycles_advanced": True,
    "hal_overload_recent": False,
    "probe_available": True,
    "default_device_transport": "bluetooth",
    "bluetooth_address": "10-94-97-1c-c6-43",
    "device_is_alive": True,
    "running_somewhere_before_probe": True,
    "sample_clock_ratio": 1.0,
    "shell_link_fault": None,
    "checked_at": "2026-11-03T15:00:00.000Z",
}

_MISSED: dict[str, Any] = {
    "verdict": "not_delivering",
    "failed_windows": 3,
    "windows_required": 3,
    "peak_correlation": 0.04,
    "mic_label": "MX Brio",
    "mic_transport": "usb",
}


class _FakeShell:
    def __init__(self, reconnect: dict[str, Any] | None = None) -> None:
        self.reconnect_calls = 0
        self._reconnect = reconnect or {"reconnected": True, "device_name": "HYPERBOOM", "elapsed_ms": 5100}

    def get_output_health(self) -> dict[str, Any]:
        return dict(_SHELL_OK)

    def post_reconnect_speaker(self) -> dict[str, Any]:
        self.reconnect_calls += 1
        return self._reconnect

    def get_input_devices(self) -> dict[str, Any]:
        return {
            "inputs": [
                {"uid": "brio", "name": "MX Brio", "transport": "usb", "channels": 2, "acoustic_proof_allowed": True},
                {
                    "uid": "sony",
                    "name": "af-sony-fun",
                    "transport": "bluetooth",
                    "channels": 1,
                    "acoustic_proof_allowed": False,
                },
            ],
            "error": None,
        }


def _app(fake: _FakeShell) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes._client] = lambda: fake
    return TestClient(app)


@pytest.mark.requirement("AUDIO-DEVICE-04")
def test_a_fresh_acoustic_miss_turns_a_shell_ok_into_not_delivering() -> None:
    """[if] a fresh acoustic miss leaves a shell OK as delivering [then] silent speakers read as fine, [else stop]."""
    with _app(_FakeShell()) as client:
        assert client.get("/api/v1/audio/output-health").json()["verdict"] == "ok", "control: no proof yet"
        posted = client.post("/api/v1/audio/acoustic-proof", json=_MISSED)
        assert posted.status_code == 200, posted.text
        body = client.get("/api/v1/audio/output-health").json()
    assert body["verdict"] == "not_delivering", "if the stalled-HYPERBOOM ok survives a mic miss then detect is broken"
    assert body["device_delivering"] is False
    assert "MX Brio" in body["reason"]
    assert body["acoustic_proof"]["failed_windows"] == 3


@pytest.mark.requirement("AUDIO-DEVICE-04")
def test_a_heard_proof_keeps_ok_and_a_bluetooth_mic_is_refused() -> None:
    """[if] a heard proof drops OK or a Bluetooth mic is trusted as proof [then] the speaker check is wrong, [else stop]."""
    with _app(_FakeShell()) as client:
        client.post("/api/v1/audio/acoustic-proof", json={**_MISSED, "verdict": "heard", "failed_windows": 0})
        assert client.get("/api/v1/audio/output-health").json()["verdict"] == "ok"
        refused = client.post("/api/v1/audio/acoustic-proof", json={**_MISSED, "mic_transport": "bluetooth"})
    assert refused.status_code == 422, "if a Bluetooth mic is accepted then HFP can wreck the speaker under test"


@pytest.mark.requirement("AUDIO-DEVICE-04")
def test_a_stale_proof_is_ignored_and_a_shell_fault_is_never_upgraded() -> None:
    """[if] a stale proof counts or a shell fault is upgraded to OK [then] the speaker light lies, [else stop]."""
    proof = AcousticProof(
        verdict="not_delivering",
        failed_windows=3,
        windows_required=3,
        peak_correlation=0.0,
        mic_label="MX Brio",
        mic_transport="usb",
        received_at="2026-11-03T15:00:00.000Z",
        received_monotonic_s=100.0,
    )
    stale = overlay_acoustic_proof(dict(_SHELL_OK), proof, 100.0 + ACOUSTIC_PROOF_TTL_S + 1)
    assert stale["verdict"] == "ok", "if a stale miss still overrides then one old window mutes the bar forever"
    assert stale["acoustic_proof"] is None
    heard = AcousticProof(**{**proof.__dict__, "verdict": "heard"})
    jack = overlay_acoustic_proof({**_SHELL_OK, "verdict": "muted_by_jack"}, heard, 101.0)
    assert jack["verdict"] == "muted_by_jack", "if a heard proof upgrades a shell fault then the overlay is broken"


@pytest.mark.requirement("AUDIO-DEVICE-04")
def test_input_devices_pass_through_with_the_mic_guard_flag() -> None:
    """[if] input devices lose the mic guard flag on the way through [then] a Bluetooth mic can be picked for proof, [else stop]."""
    with _app(_FakeShell()) as client:
        body = client.get("/api/v1/audio/input-devices").json()
    allowed = {row["name"]: row["acoustic_proof_allowed"] for row in body["inputs"]}
    assert allowed == {"MX Brio": True, "af-sony-fun": False}


@pytest.mark.requirement("AUDIO-DEVICE-05")
def test_reconnect_speaker_returns_the_shell_result_and_503s_on_failure() -> None:
    """[if] reconnect-speaker hides the shell result or answers 200 on failure [then] agents cannot tell it failed, [else stop]."""
    fake = _FakeShell()
    with _app(fake) as client:
        ok = client.post("/api/v1/audio/reconnect-speaker")
    assert ok.status_code == 200
    assert ok.json()["reconnected"] is True
    assert fake.reconnect_calls == 1
    missing = _FakeShell({"reconnected": False, "error": "blueutil not found at /opt/homebrew/bin/blueutil"})
    with _app(missing) as client:
        failed = client.post("/api/v1/audio/reconnect-speaker")
    assert failed.status_code == 503, "if a failed reconnect is a 200 then the UI claims a fix that did not happen"
    assert "blueutil not found" in failed.json()["detail"]["error"]
