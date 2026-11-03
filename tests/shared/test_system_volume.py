"""The system volume seam, run live on the arm this host has.

Each platform arm drives the real OS mixer. The Windows test changes the
master volume and restores the original in ``finally``; the macOS and Linux
arms skip elsewhere, naming the program they need.
"""

from __future__ import annotations

import re
import shutil
import sys

import pytest

from apps.shared import system_volume
from apps.shared.system_volume import OSASCRIPT_PATH, check_percent, get_volume, set_volume


def _other_percent(current: int) -> int:
    return current + 7 if current <= 90 else current - 7


def _round_trip(expected_backend: str) -> None:
    original = get_volume()
    assert original.backend == expected_backend
    assert 0 <= original.volume <= 100
    target = _other_percent(original.volume)
    try:
        after = set_volume(target)
        assert after.volume == target
        assert get_volume().volume == target
        assert after.muted == original.muted
    finally:
        restored = set_volume(original.volume)
    assert restored.volume == original.volume


@pytest.mark.skipif(sys.platform != "win32", reason="Core Audio via %SystemRoot% powershell.exe is Windows-only")
def test_windows_core_audio_round_trip_restores_original() -> None:
    _round_trip("core_audio")


@pytest.mark.skipif(
    sys.platform != "darwin" or not OSASCRIPT_PATH.is_file(),
    reason="needs macOS /usr/bin/osascript ('get volume settings')",
)
def test_macos_osascript_round_trip_restores_original() -> None:
    _round_trip("osascript")


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not (shutil.which("wpctl") or shutil.which("pactl")),
    reason="needs Linux wpctl (PipeWire) or pactl (PulseAudio) with a default sink",
)
def test_linux_mixer_round_trip_restores_original() -> None:
    tool = "wpctl" if shutil.which("wpctl") else "pactl"
    _skip_unless_a_sound_server_answers(tool)
    _round_trip(tool)


def _skip_unless_a_sound_server_answers(tool: str) -> None:
    """A headless runner ships pactl or wpctl without a running server: skip, naming it.

    Only the tool's own failure to reach its server is a skip (``<tool>_failed:...``,
    seen live as ``pactl_failed:Connection failure: Connection refused`` on CI runners
    with no PulseAudio). Any other reason is a code defect and still fails.
    """
    try:
        get_volume()
    except system_volume.SystemVolumeUnavailable as exc:
        if not exc.reason.startswith(f"{tool}_failed:"):
            raise
        pytest.skip(f"{tool} is on PATH but no sound server answers on this host: {exc.reason}")


@pytest.mark.parametrize("bad", [-1, 101, 50.0, "50", True, None])
def test_check_percent_refuses_non_percent(bad: object) -> None:
    with pytest.raises(ValueError, match="integer percent"):
        check_percent(bad)


def test_set_volume_refuses_before_touching_the_mixer() -> None:
    with pytest.raises(ValueError):
        set_volume(150)


# The Linux arm over canned mixer output: no subprocess runs, so no real volume moves.
_WPCTL = ("wpctl", {"get-volume": "Volume: {:.2f}\n"})
_PACTL = ("pactl", {"get-sink-volume": "Volume: front-left: 1 / {:.0f}% / 0.00 dB\n", "get-sink-mute": "Mute: no\n"})


def _linux_reading(
    monkeypatch: pytest.MonkeyPatch, tool: tuple[str, dict[str, str]], percent: int
) -> system_volume.SystemVolume:
    name, replies = tool
    scale = 0.01 if name == "wpctl" else 1
    monkeypatch.setattr(system_volume, "_linux_tool", lambda: (name, f"/opt/bin/{name}"))
    monkeypatch.setattr(system_volume, "_run", lambda argv: replies[argv[1]].format(percent * scale))
    return system_volume._linux_get()


@pytest.mark.parametrize("tool", [_WPCTL, _PACTL], ids=["wpctl", "pactl"])
@pytest.mark.parametrize("percent", [101, 150])
def test_a_linux_sink_above_100_is_refused_with_its_reading(
    monkeypatch: pytest.MonkeyPatch, tool: tuple[str, dict[str, str]], percent: int
) -> None:
    """[if] a boosted sink returns as is [then] the 0..100 route answers an unstructured 500."""
    with pytest.raises(system_volume.SystemVolumeUnavailable) as caught:
        _linux_reading(monkeypatch, tool, percent)
    assert caught.value.reason == f"sink_above_100_percent:{percent}"


@pytest.mark.parametrize("tool", [_WPCTL, _PACTL], ids=["wpctl", "pactl"])
@pytest.mark.parametrize("percent", [0, 100])
def test_a_linux_sink_inside_the_contract_reads_exactly(
    monkeypatch: pytest.MonkeyPatch, tool: tuple[str, dict[str, str]], percent: int
) -> None:
    """[if] the refusal overshoots [then] a sink at full volume stops reading at all."""
    reading = _linux_reading(monkeypatch, tool, percent)
    assert (reading.volume, reading.muted, reading.backend) == (percent, False, tool[0])


def test_unavailable_carries_its_reason_and_code() -> None:
    exc = system_volume.SystemVolumeUnavailable("neither_wpctl_nor_pactl_on_path")
    assert exc.code == "system_volume_unavailable"
    assert exc.reason == "neither_wpctl_nor_pactl_on_path"


@pytest.mark.parametrize("reply", ["", "Mute: maybe\n", "Stumm: ja\n", "yes\n", "Mute: no\nMute: yes\n"])
def test_unparseable_pactl_mute_output_is_refused(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    """[if] any reply without "yes" reads as unmuted [then] the backend reports a state it never read."""
    replies = {**_PACTL[1], "get-sink-mute": reply}
    with pytest.raises(system_volume.SystemVolumeUnavailable) as caught:
        _linux_reading(monkeypatch, ("pactl", replies), 50)
    assert caught.value.reason == "pactl_mute_unparseable"


@pytest.mark.parametrize(("reply", "muted"), [("Mute: yes\n", True), ("Mute: no\n", False)])
def test_pactl_mute_reads_both_states(monkeypatch: pytest.MonkeyPatch, reply: str, muted: bool) -> None:
    """[if] the strict parse overshoots [then] a real muted or unmuted sink stops reading."""
    replies = {**_PACTL[1], "get-sink-mute": reply}
    assert _linux_reading(monkeypatch, ("pactl", replies), 50).muted is muted


@pytest.mark.requirement("AISIDE-7892d")
def test_win_core_audio_methods_preserve_hresult_signatures() -> None:
    """[if] a _WIN_CORE_AUDIO method returns int [then] it is marked [PreserveSig], [else stop]."""
    pattern = re.compile(r"(?:\[PreserveSig\]\s*)?int\s+\w+\s*\(")
    matches = list(pattern.finditer(system_volume._WIN_CORE_AUDIO))
    assert len(matches) == 16
    for match in matches:
        assert match.group(0).startswith("[PreserveSig]")
