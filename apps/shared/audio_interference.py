"""Which installed software is likely to fight Open DJ for the microphone.

Cue alignment plays a probe out of one device and listens for it on another.
That measurement is destroyed by anything else that opens the input, because
opening a Bluetooth headset's microphone drops the link from A2DP to HFP: the
sweep is then narrowband and heavily compressed, and the correlation collapses
from ~0.6 to ~0.1 without the bus ever going quiet.

Measured on silver, Wed 16 Sep 2026, same sweep and same device each time:

    system input = a wired USB mic (A2DP holds)   0.54  0.65  0.59  0.56  0.56
    system input = the headset itself (HFP)       0.00  0.06  0.04  0.11  0.07

Fathom's always-on monitor re-claimed the input every ~13 seconds, and pushing
the default back was not enough, because setting the default does not close a
stream another process already holds.

This module is a HEURISTIC and says so in its wire contract. It reports what is
installed or running; it cannot observe who owns the device. Overclaiming here
would be worse than silence, because an operator who is told "Fathom is holding
your microphone" and quits it, with no change, stops believing the next warning.

`detect_audio_interference` is pure so the rule can be tested without a machine
in a particular state; the live readers live in the API module.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

# Our own bundle must never be reported: warning the operator about the app
# they are standing in is noise, and it always matches.
SELF_TOKENS: tuple[str, ...] = ("open dj", "opendj")


class AudioGrabberKind(Enum):
    """How the thing was found, which decides how it is worded."""

    RUNNING_APP = "running_app"
    AUDIO_DRIVER = "audio_driver"


@dataclass(frozen=True)
class AudioGrabber:
    key: str
    label: str
    tokens: tuple[str, ...]
    why: str


@dataclass(frozen=True)
class AudioInterference:
    key: str
    label: str
    kind: AudioGrabberKind
    why: str
    matched: str


#: Meeting recorders and audio processors that keep an input stream open, or
#: install a CoreAudio plugin that is loaded whether or not the app is running.
#: Tokens are matched case-insensitively against process paths and driver
#: names, and are deliberately long enough not to collide with ordinary
#: software (see the token-length test).
KNOWN_AUDIO_GRABBERS: tuple[AudioGrabber, ...] = (
    AudioGrabber(
        key="fathom",
        label="Fathom",
        tokens=("fathom",),
        why="its meeting monitor keeps an input stream open even when no meeting is running",
    ),
    AudioGrabber(
        key="granola",
        label="Granola",
        tokens=("granola",),
        why="it listens for meetings in the background",
    ),
    AudioGrabber(
        key="krisp",
        label="Krisp",
        tokens=("krisp",),
        why=(
            "it installs a CoreAudio plugin that processes the input, "
            "loaded whether or not the app is open"
        ),
    ),
    AudioGrabber(
        key="otter",
        label="Otter.ai",
        tokens=("otter.ai", "otterai"),
        why="it records meetings in the background",
    ),
    AudioGrabber(
        key="zoom",
        label="Zoom",
        tokens=("zoom.us", "zoomclips"),
        why="it can hold the microphone between calls",
    ),
    AudioGrabber(
        key="teams",
        label="Microsoft Teams",
        tokens=("microsoft teams",),
        why="it can hold the microphone between calls",
    ),
    AudioGrabber(
        key="loom",
        label="Loom",
        tokens=("loom.app", "loomdesktop"),
        why="its recorder can keep the microphone open while idle",
    ),
    AudioGrabber(
        key="descript",
        label="Descript",
        tokens=("descript",),
        why="it can hold the microphone while a project is open",
    ),
)


def _is_self(haystack: str) -> bool:
    return any(token in haystack for token in SELF_TOKENS)


def _match(
    names: Iterable[str], kind: AudioGrabberKind
) -> dict[str, AudioInterference]:
    found: dict[str, AudioInterference] = {}
    for name in names:
        haystack = name.lower()
        if _is_self(haystack):
            continue
        for grabber in KNOWN_AUDIO_GRABBERS:
            if grabber.key in found:
                continue
            if any(token in haystack for token in grabber.tokens):
                found[grabber.key] = AudioInterference(
                    key=grabber.key,
                    label=grabber.label,
                    kind=kind,
                    why=grabber.why,
                    matched=name,
                )
    return found


def detect_audio_interference(
    *,
    process_names: Sequence[str],
    driver_names: Sequence[str],
) -> tuple[AudioInterference, ...]:
    """Everything recognised, at most once each, in a stable order.

    A CoreAudio driver outranks a running process for the same product: the
    plugin is loaded by coreaudiod regardless of the app, so it is the more
    durable fact and the one the operator has to act on differently.
    """
    by_key = _match(process_names, AudioGrabberKind.RUNNING_APP)
    by_key.update(_match(driver_names, AudioGrabberKind.AUDIO_DRIVER))
    return tuple(by_key[key] for key in sorted(by_key))


__all__ = [
    "KNOWN_AUDIO_GRABBERS",
    "AudioGrabber",
    "AudioGrabberKind",
    "AudioInterference",
    "detect_audio_interference",
]
