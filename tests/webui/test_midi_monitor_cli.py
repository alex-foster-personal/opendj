"""opendj midi monitor resolves recorded bytes against the exported maps (#10307)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.opendj_cli import EXIT_FAILED, midi_cli

_MAPS = Path(__file__).resolve().parents[2] / "apps" / "audio-engine" / "maps" / "device-maps.json"
_RISING = (6, 8, 9, 11, 13)


def _cc(channel: int, controller: int, value: int) -> list[int]:
    return [0xB0 | (channel - 1), controller, value]


def _note_on(channel: int, note: int, velocity: int = 127) -> list[int]:
    return [0x90 | (channel - 1), note, velocity]


def _session_lines(messages: list[list[int]], device: str = "XDJ-AZ") -> list[str]:
    maps = midi_cli.load_builtin_maps(_MAPS)
    device_map = midi_cli.choose_map(maps, device)
    assert device_map is not None
    session = midi_cli.MonitorSession(index=midi_cli.index_bindings(device_map))
    lines = [session.handle(message) for message in messages]
    return [line for line in lines if line is not None]


def _verdicts(messages: list[list[int]], device: str = "XDJ-AZ") -> tuple[list[str], list[str]]:
    maps = midi_cli.load_builtin_maps(_MAPS)
    device_map = midi_cli.choose_map(maps, device)
    assert device_map is not None
    session = midi_cli.MonitorSession(index=midi_cli.index_bindings(device_map))
    for message in messages:
        session.handle(message)
    return session.fader_verdicts(device_map), session.unseen(device_map)


def test_rising_ch5_cc17_is_ch1_fader_normal() -> None:
    """[if] CC 17 on ch 5 rises 6, 8, 9, 11, 13 [then] ch1 fader: rising (normal)."""
    messages = [_cc(5, 17, value) for value in _RISING]
    lines = _session_lines(messages)
    assert lines
    for line, value in zip(lines, _RISING, strict=True):
        assert "unmapped" not in line
        assert "mixer_channel" in line
        assert "deck=1" in line
        assert f"cc ch5 17 {value}" in line
    verdicts, unseen = _verdicts(messages)
    assert "ch1 fader: rising (normal)" in verdicts
    assert "cc ch5 17 mixer_channel deck=1" not in unseen


def test_falling_ch5_cc17_is_ch1_fader_inverted() -> None:
    """[if] CC 17 on ch 5 falls 13, 11, 9, 8, 6 [then] ch1 fader: falling (inverted)."""
    messages = [_cc(5, 17, value) for value in reversed(_RISING)]
    verdicts, _unseen = _verdicts(messages)
    assert "ch1 fader: rising (normal)" not in verdicts
    assert "ch1 fader: falling (inverted)" in verdicts


def test_unmapped_note_prints_channel_and_number() -> None:
    """[if] a note the map does not bind arrives [then] the line is unmapped with ch and number."""
    lines = _session_lines([_note_on(16, 99)])
    assert lines == ["note ch16 99 unmapped"]


def test_bound_note_prints_action_type_and_deck() -> None:
    """[if] a bound note arrives [then] the line names the action type and deck."""
    lines = _session_lines([_note_on(1, 0)])
    assert len(lines) == 1
    assert "unmapped" not in lines[0]
    assert "deck_play_toggle" in lines[0]
    assert "deck=1" in lines[0]
    assert "note ch1 0" in lines[0]


def test_no_matching_device_refuses_with_the_device_list(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] --device matches no port [then] refuse with the device list and EXIT_FAILED."""
    code = midi_cli.run(
        ["monitor", "--device", "NoSuchController"],
        list_ports=lambda: ["XDJ-AZ", "DDJ-FLX4"],
    )
    assert code == EXIT_FAILED
    err = capsys.readouterr().err
    assert "NoSuchController" in err
    assert "XDJ-AZ" in err
    assert "DDJ-FLX4" in err
    assert err.startswith("opendj:")


def test_ambiguous_device_refuses_with_just_the_hits(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] --device matches two ports [then] refuse as ambiguous and list only the hits."""
    code = midi_cli.run(
        ["monitor", "--device", "XDJ"],
        list_ports=lambda: ["XDJ-AZ", "XDJ-RX3", "DDJ-FLX4"],
    )
    assert code == EXIT_FAILED
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "matches 2 ports" in err
    assert "XDJ-AZ" in err
    assert "XDJ-RX3" in err
    assert "DDJ-FLX4" not in err
    assert "NoSuchController" not in err
    assert err.startswith("opendj:")


def test_run_feed_prints_bound_lines_then_verdicts(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] recorded bytes are fed to run() [then] each line and the fader verdict print."""
    code = midi_cli.run(
        ["monitor", "--device", "XDJ-AZ"],
        list_ports=lambda: [],
        maps_path=_MAPS,
        feed=[_cc(5, 17, value) for value in _RISING],
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "mixer_channel deck=1" in out
    assert "ch1 fader: rising (normal)" in out
    assert "unseen:" in out
