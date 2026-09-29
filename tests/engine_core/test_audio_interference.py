"""CUEOUT-21: warn when another app is likely holding the microphone.

Built from a measured failure (Wed 16 Sep 2026). Cue alignment could not
calibrate a Bluetooth headset: macOS kept re-selecting the headset as the
system INPUT, which drops the link from A2DP to HFP, and the probe sweep does
not survive HFP. Same sweep, same device, same mic: 0.54-0.65 per burst while
A2DP held, 0.00-0.11 after the flip. The app correctly measured 0.09 and then
blamed the microphone's position, which sent the operator to move hardware that
was already correct.

Fathom's always-on FathomAudioMonitor was re-claiming the input every ~13
seconds. Krisp ships a CoreAudio HAL plugin that is loaded whether or not its
app is open. Neither is visible to a web page, so the engine has to say it.

This is a HEURISTIC and the tests hold it to that: it reports what is running,
never that a given app is holding the device right now.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.audio_interference_api import (
    AUDIO_INTERFERENCE_PATH,
    add_audio_interference_route,
)
from apps.shared.audio_interference import (
    KNOWN_AUDIO_GRABBERS,
    AudioGrabberKind,
    detect_audio_interference,
)

# Real process names, copied from `ps aux` on silver while the fault was live.
FATHOM_PROCESSES = (
    "/Applications/Fathom.app/Contents/Resources/FathomMeetingMonitor",
    "/Applications/Fathom.app/Contents/Resources/FathomAudioMonitor",
    "/Applications/Fathom.app/Contents/Frameworks/Fathom Helper",
)
# An ordinary busy Mac with nothing that touches the mic.
INNOCENT_PROCESSES = (
    "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder",
    "/System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/opt/homebrew/bin/node",
    "/usr/bin/python3",
    "/Applications/Open DJ.app/Contents/MacOS/Open DJ",
)


def test_fathom_running_is_reported() -> None:
    found = detect_audio_interference(process_names=FATHOM_PROCESSES, driver_names=())
    assert [d.key for d in found] == ["fathom"], found
    assert found[0].kind is AudioGrabberKind.RUNNING_APP


def test_krisp_audio_driver_is_reported_even_with_no_app_running() -> None:
    """The HAL plugin is loaded by coreaudiod, so the app need not be open."""
    found = detect_audio_interference(
        process_names=INNOCENT_PROCESSES,
        driver_names=("KrispAudio.driver",),
    )
    assert [d.key for d in found] == ["krisp"], found
    assert found[0].kind is AudioGrabberKind.AUDIO_DRIVER


def test_an_ordinary_machine_reports_nothing() -> None:
    """The control. Without it a rule that fires on everything would pass."""
    assert detect_audio_interference(process_names=INNOCENT_PROCESSES, driver_names=()) == ()


def test_open_dj_never_reports_itself() -> None:
    """Warning the operator about the app they are standing in is noise.

    Our own bundle is the one path guaranteed to be running during a
    calibration, so the guard is exercised with a path that WOULD otherwise
    match. A bundle whose name collides with no token tests nothing.
    """
    found = detect_audio_interference(
        process_names=(
            "/Applications/Open DJ.app/Contents/MacOS/Open DJ",
            "/Applications/Open DJ.app/Contents/Frameworks/krisp-denoise-shim",
        ),
        driver_names=(),
    )
    assert found == (), "a matching name inside our own bundle must not be reported"


def test_each_detection_is_reported_once_however_many_processes_match() -> None:
    found = detect_audio_interference(process_names=FATHOM_PROCESSES * 3, driver_names=())
    assert len(found) == 1, found


def test_several_offenders_are_all_reported_in_a_stable_order() -> None:
    found = detect_audio_interference(
        process_names=(*FATHOM_PROCESSES, "/Applications/Granola.app/Contents/MacOS/Granola"),
        driver_names=("KrispAudio.driver",),
    )
    assert [d.key for d in found] == sorted(d.key for d in found), "order must be deterministic"
    assert {"fathom", "granola", "krisp"} == {d.key for d in found}


def test_matching_is_case_insensitive() -> None:
    assert detect_audio_interference(process_names=("/tmp/FATHOMAUDIOMONITOR",), driver_names=())


def test_every_known_grabber_carries_operator_facing_copy() -> None:
    for grabber in KNOWN_AUDIO_GRABBERS:
        assert grabber.label.strip() != ""
        assert grabber.why.strip() != "", grabber.key
        assert grabber.tokens, grabber.key


def test_no_grabber_token_is_short_enough_to_collide() -> None:
    """A two-letter token would match half of /Applications."""
    for grabber in KNOWN_AUDIO_GRABBERS:
        for token in grabber.tokens:
            assert len(token) >= 5, (grabber.key, token)


def test_detection_never_claims_the_device_is_held() -> None:
    """It cannot know that, so it must not say it."""
    found = detect_audio_interference(process_names=FATHOM_PROCESSES, driver_names=())
    wording = (found[0].why + found[0].label).lower()
    for forbidden in ("is holding", "has taken", "is using the microphone"):
        assert forbidden not in wording


def test_http_route_reports_the_injected_state() -> None:
    app = FastAPI()
    add_audio_interference_route(
        app,
        read_processes=lambda: FATHOM_PROCESSES,
        read_audio_drivers=lambda: ("KrispAudio.driver",),
    )
    body = TestClient(app).get(AUDIO_INTERFERENCE_PATH).json()
    assert body["supported"] is True
    assert {d["key"] for d in body["detected"]} == {"fathom", "krisp"}


def test_http_route_on_a_clean_machine_is_empty_not_absent() -> None:
    app = FastAPI()
    add_audio_interference_route(
        app,
        read_processes=lambda: INNOCENT_PROCESSES,
        read_audio_drivers=lambda: (),
    )
    body = TestClient(app).get(AUDIO_INTERFERENCE_PATH).json()
    assert body == {"supported": True, "detected": [], "error": None}


def test_http_route_reports_unsupported_rather_than_clean_when_it_cannot_look() -> None:
    """UNKNOWN is never collapsed into a pass: a probe that could not run must
    not render as 'nothing is interfering'."""
    def boom() -> tuple[str, ...]:
        raise OSError("process list unavailable")

    app = FastAPI()
    add_audio_interference_route(app, read_processes=boom, read_audio_drivers=lambda: ())
    body = TestClient(app).get(AUDIO_INTERFERENCE_PATH).json()
    assert body["supported"] is False
    assert body["detected"] == []
    assert "process list unavailable" in body["error"]
