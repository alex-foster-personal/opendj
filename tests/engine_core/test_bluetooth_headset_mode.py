"""Warn when Bluetooth headphones are in, or one app away from, call mode.

Opening a Bluetooth headset's microphone drops the link from A2DP (wideband
stereo) to HFP (mono, 8 to 24 kHz), and macOS often makes a newly connected
headset the system input. The readings below are modeled on silver's own
Sony headphones, read live Tue 3 Nov 2026: one CoreAudio object per direction,
both named 'af-sony-fun', the output at 44.1 kHz.
"""

from __future__ import annotations

import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.bluetooth_headset_mode_api import (
    AUDIO_HEADSET_MODE_PATH,
    add_audio_headset_mode_route,
    read_default_devices,
)
from apps.shared.bluetooth_headset_mode import (
    AudioDeviceReading,
    HeadsetModeVerdict,
    classify_headset_mode,
)

SONY_OUT = AudioDeviceReading(166, "af-sony-fun", "bluetooth", 44_100.0)
SONY_OUT_HFP = AudioDeviceReading(166, "af-sony-fun", "bluetooth", 16_000.0)
SONY_MIC = AudioDeviceReading(171, "af-sony-fun", "bluetooth", 16_000.0)
USB_MIC = AudioDeviceReading(158, "USB MICROPHONE", "usb", 48_000.0)
SPEAKERS = AudioDeviceReading(90, "Mac Studio Speakers", "built_in", 48_000.0)
AIRPODS = AudioDeviceReading(200, "the maintainer's AirPods", "bluetooth", 48_000.0)


def test_bluetooth_output_below_a2dp_rate_is_headset_mode() -> None:
    assert classify_headset_mode(SONY_OUT_HFP, USB_MIC) is HeadsetModeVerdict.HEADSET_MODE


def test_every_hfp_rate_is_headset_mode_and_every_a2dp_rate_is_not() -> None:
    for rate in (8_000.0, 16_000.0, 24_000.0):
        out = AudioDeviceReading(1, "phones", "bluetooth_le", rate)
        assert classify_headset_mode(out, USB_MIC) is HeadsetModeVerdict.HEADSET_MODE, rate
    for rate in (44_100.0, 48_000.0):
        out = AudioDeviceReading(1, "phones", "bluetooth", rate)
        assert classify_headset_mode(out, USB_MIC) is HeadsetModeVerdict.CLEAR, rate


def test_headset_mic_as_system_input_is_mic_selected_across_split_objects() -> None:
    """Sony exposes input and output as two objects sharing one name."""
    assert classify_headset_mode(SONY_OUT, SONY_MIC) is HeadsetModeVerdict.MIC_SELECTED


def test_headset_mic_on_the_same_object_is_mic_selected() -> None:
    assert classify_headset_mode(AIRPODS, AIRPODS) is HeadsetModeVerdict.MIC_SELECTED


def test_silver_as_read_live_is_clear() -> None:
    assert classify_headset_mode(SONY_OUT, USB_MIC) is HeadsetModeVerdict.CLEAR


def test_a_different_headsets_mic_does_not_warn_about_these_headphones() -> None:
    assert classify_headset_mode(SONY_OUT, AIRPODS) is HeadsetModeVerdict.CLEAR


def test_low_rate_wired_output_is_not_mistaken_for_call_mode() -> None:
    usb_dac = AudioDeviceReading(5, "USB DAC", "usb", 16_000.0)
    assert classify_headset_mode(usb_dac, SONY_MIC) is HeadsetModeVerdict.CLEAR
    assert classify_headset_mode(SPEAKERS, SONY_MIC) is HeadsetModeVerdict.CLEAR


def test_no_output_reading_is_unsupported_never_clear() -> None:
    assert classify_headset_mode(None, USB_MIC) is HeadsetModeVerdict.UNSUPPORTED


def _client(reader) -> TestClient:
    app = FastAPI()
    add_audio_headset_mode_route(app, read_devices=reader)
    return TestClient(app)


def test_route_reports_headset_mode_with_both_devices() -> None:
    body = _client(lambda: (SONY_OUT_HFP, USB_MIC)).get(AUDIO_HEADSET_MODE_PATH).json()
    assert body["supported"] is True
    assert body["verdict"] == "headset_mode"
    assert body["output"]["name"] == "af-sony-fun"
    assert body["output"]["nominal_sample_rate_hz"] == 16_000.0
    assert body["input"]["transport"] == "usb"


def test_route_turns_a_failed_read_into_unsupported_with_the_reason() -> None:
    def refuse():
        raise OSError("headset-mode detection reads CoreAudio and needs macOS, not linux")

    body = _client(refuse).get(AUDIO_HEADSET_MODE_PATH).json()
    assert body == {
        "supported": False,
        "verdict": "unsupported",
        "output": None,
        "input": None,
        "error": "headset-mode detection reads CoreAudio and needs macOS, not linux",
    }


def test_route_with_no_default_output_is_unsupported_and_says_why() -> None:
    body = _client(lambda: (None, USB_MIC)).get(AUDIO_HEADSET_MODE_PATH).json()
    assert body["supported"] is False
    assert body["verdict"] == "unsupported"
    assert body["error"] == "no default output device"


# --- the real CoreAudio boundary: no injected reader below this line ------

KNOWN_TRANSPORTS = {
    "bluetooth",
    "bluetooth_le",
    "built_in",
    "usb",
    "hdmi",
    "displayport",
    "airplay",
    "virtual",
    "aggregate",
    "thunderbolt",
}


@pytest.mark.skipif(sys.platform != "darwin", reason="UNAVAILABLE: CoreAudio exists only on macOS")
def test_real_reader_reads_the_default_devices_through_coreaudio() -> None:
    """A wrong selector or ctypes signature raises or returns garbage here."""
    output, input_device = read_default_devices()
    if output is None:
        pytest.skip("UNAVAILABLE: this Mac has no default output device to read")
    assert output.device_id > 0
    assert output.name.strip() != ""
    # An unknown four-char code would still be 4 characters; a garbage read is not.
    assert output.transport in KNOWN_TRANSPORTS or len(output.transport) == 4, output
    assert 8_000.0 <= output.nominal_sample_rate_hz <= 768_000.0, output
    if input_device is not None:
        assert input_device.device_id > 0
        assert input_device.nominal_sample_rate_hz > 0


@pytest.mark.skipif(sys.platform != "darwin", reason="UNAVAILABLE: CoreAudio exists only on macOS")
def test_real_route_on_macos_reports_a_verdict_not_unsupported() -> None:
    app = FastAPI()
    add_audio_headset_mode_route(app)
    body = TestClient(app).get(AUDIO_HEADSET_MODE_PATH).json()
    if body["error"] == "no default output device":
        pytest.skip("UNAVAILABLE: this Mac has no default output device to read")
    assert body["supported"] is True, body
    assert body["verdict"] in {"headset_mode", "mic_selected", "clear"}, body


@pytest.mark.skipif(sys.platform == "darwin", reason="covered by the macOS real-path tests above")
def test_real_route_off_macos_says_it_could_not_look() -> None:
    app = FastAPI()
    add_audio_headset_mode_route(app)
    body = TestClient(app).get(AUDIO_HEADSET_MODE_PATH).json()
    assert body["supported"] is False
    assert body["verdict"] == "unsupported"
    assert "needs macOS" in body["error"]
