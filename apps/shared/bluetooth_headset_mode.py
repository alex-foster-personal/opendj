"""Is the operator's Bluetooth headphone stuck in headset (call) mode?

A Bluetooth headphone plays music over A2DP, which is wideband stereo. The
moment any app opens its MICROPHONE, macOS drops the link to HFP, the call
profile: mono, 8 to 24 kHz, the "tinny" sound. macOS also tends to make a
newly connected headset the system INPUT, so a meeting app, a dictation
helper, or Open DJ's own cue calibration flips the headphones without the
operator touching anything (CUEOUT-21 measured the same flip, Wed 16 Sep 2026).

Two states are worth a warning, and they read differently:

- HEADSET_MODE: the default output is a Bluetooth device whose nominal sample
  rate has dropped below `A2DP_MIN_SAMPLE_RATE_HZ`. HFP runs at 8, 16 or 24 kHz;
  A2DP at 44.1 or 48 kHz. This is the flip itself, happening now.
- MIC_SELECTED: the headphones still play wideband, but their own mic is the
  system input, so the next app that listens flips them.

`classify_headset_mode` is pure so the rule is testable without a Mac in a
particular state; the CoreAudio reader lives in the API module. A reading
that could not be taken is UNSUPPORTED, never CLEAR.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Lowest rate an A2DP stream runs at. Anything below it on a Bluetooth output
#: is the HFP call profile (8, 16 or 24 kHz).
A2DP_MIN_SAMPLE_RATE_HZ: float = 32_000.0

#: CoreAudio `kAudioDevicePropertyTransportType` values for Bluetooth
#: ('blue' classic, 'blea' Low Energy), as the API module names them.
BLUETOOTH_TRANSPORTS: frozenset[str] = frozenset({"bluetooth", "bluetooth_le"})


class HeadsetModeVerdict(Enum):
    HEADSET_MODE = "headset_mode"
    MIC_SELECTED = "mic_selected"
    CLEAR = "clear"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class AudioDeviceReading:
    """One CoreAudio default device, as read; nothing here is inferred."""

    device_id: int
    name: str
    transport: str
    nominal_sample_rate_hz: float


def _is_bluetooth(device: AudioDeviceReading | None) -> bool:
    return device is not None and device.transport in BLUETOOTH_TRANSPORTS


def _same_device(a: AudioDeviceReading, b: AudioDeviceReading) -> bool:
    # A headset is usually one CoreAudio object with both directions; some
    # stacks split it into two objects that share the device's name.
    return a.device_id == b.device_id or (a.name != "" and a.name == b.name)


def classify_headset_mode(
    output: AudioDeviceReading | None, input_device: AudioDeviceReading | None
) -> HeadsetModeVerdict:
    """The verdict for one reading of the system default output and input."""
    if output is None:
        return HeadsetModeVerdict.UNSUPPORTED
    if not _is_bluetooth(output):
        return HeadsetModeVerdict.CLEAR
    if 0 < output.nominal_sample_rate_hz < A2DP_MIN_SAMPLE_RATE_HZ:
        return HeadsetModeVerdict.HEADSET_MODE
    if input_device is not None and _is_bluetooth(input_device) and _same_device(output, input_device):
        return HeadsetModeVerdict.MIC_SELECTED
    return HeadsetModeVerdict.CLEAR


__all__ = [
    "A2DP_MIN_SAMPLE_RATE_HZ",
    "BLUETOOTH_TRANSPORTS",
    "AudioDeviceReading",
    "HeadsetModeVerdict",
    "classify_headset_mode",
]
