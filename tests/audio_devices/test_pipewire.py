"""pw-dump JSON parsed into the output-device list (NAE-12246).

[if] pw-dump lists audio sinks [then] each becomes one output with stable id and channels, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.audio_devices.pipewire import OutputDevice, PipeWireParseError, parse_pw_dump

pytestmark = pytest.mark.requirement("NAE-12246")

# Synthesized in the pw-dump format (not captured on a real Linux host): two sinks,
# a duplicate of the first sink, and non-audio-output nodes and objects.
FIXTURE: Path = Path(__file__).resolve().parent / "fixtures" / "pw_dump_agentbox.json"


def _sink(**props: object) -> dict[str, object]:
    base: dict[str, object] = {
        "media.class": "Audio/Sink",
        "node.name": "alsa_output.test",
        "node.description": "Test Sink",
        "audio.channels": 2,
        "audio.position": "FL,FR",
    }
    base.update(props)
    return {
        "id": 9,
        "type": "PipeWire:Interface:Node",
        "info": {"props": {k: v for k, v in base.items() if v is not None}},
    }


def test_sinks_keep_stable_identity_and_channels() -> None:
    """[if] pw-dump has an audio sink with channel metadata [then] its id and channels are listed, [else stop]."""
    devices = parse_pw_dump(FIXTURE.read_text(encoding="utf-8"))
    assert devices == [
        OutputDevice("alsa_output.pci-0000_00_1f.3.analog-stereo", "Built-in Audio Analog Stereo", 2, ("FL", "FR")),
        OutputDevice(
            "alsa_output.usb-Pioneer_DDJ-FLX4-00.analog-surround-40",
            "DDJ-FLX4 Analog Surround 4.0",
            4,
            ("FL", "FR", "RL", "RR"),
        ),
    ]


def test_non_audio_output_nodes_are_not_listed() -> None:
    """[if] pw-dump has non-audio-output nodes [then] none is a selectable output, [else stop]."""
    ids = {device.id for device in parse_pw_dump(FIXTURE.read_text(encoding="utf-8"))}
    assert not ids & {"alsa_input.pci-0000_00_1f.3.analog-stereo", "Firefox", "v4l2_input.platform-usb", "Midi-Bridge"}


@pytest.mark.parametrize(
    "dump",
    [
        "not json",
        json.dumps({"id": 1}),
        json.dumps([_sink(**{"node.name": None})]),
        json.dumps([_sink(**{"node.name": ""})]),
        json.dumps([_sink(**{"audio.channels": None})]),
        json.dumps([_sink(**{"audio.channels": 0})]),
        json.dumps([_sink(**{"audio.channels": "2"})]),
        json.dumps([_sink(**{"audio.position": "FL"})]),
        json.dumps([{"id": 9, "type": "PipeWire:Interface:Node", "info": {}}]),
    ],
)
def test_malformed_required_fields_raise(dump: str) -> None:
    """[if] required PipeWire fields are malformed [then] parsing raises, no device, [else stop]."""
    with pytest.raises(PipeWireParseError):
        parse_pw_dump(dump)


def test_duplicate_nodes_list_one_device() -> None:
    """[if] duplicate nodes share one stable sink name [then] it is listed once, [else stop]."""
    devices = parse_pw_dump(FIXTURE.read_text(encoding="utf-8"))
    assert [d.id for d in devices].count("alsa_output.pci-0000_00_1f.3.analog-stereo") == 1
    with pytest.raises(PipeWireParseError):
        parse_pw_dump(json.dumps([_sink(), _sink(**{"audio.channels": 1, "audio.position": "MONO"})]))
