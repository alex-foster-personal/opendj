"""Write the known-key WAV the core tests analyze end to end.

24 s of C major (8B), I-V-I-IV-I-V with a bass note, 22050 Hz mono 16-bit,
standard library only so CI on any platform can make it:

    python apps/track-catcher-mac/scripts/make_test_wav.py OUT.wav

EngineProcessTests.testRealEngineOnAFileWhenAvailable reads it through
TC_TEST_WAV and expects the summary key 8B.
"""

from __future__ import annotations

import math
import struct
import sys
import wave
from pathlib import Path

RATE = 22050
ARGC = 2  # script name plus the output path
SECONDS = 24
# MIDI notes per bar: C, G, C, F, C, G (major), tonic-heavy so the key is
# unambiguous against its neighbors 7B (F major) and 9B (G major).
C, G, F = (36, 60, 64, 67), (43, 59, 62, 67), (41, 60, 65, 69)
CHORDS = [C, G, C, F, C, G]


def hz(midi: int) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def samples() -> list[int]:
    out = []
    per_chord = RATE * SECONDS // (len(CHORDS) * 2)  # two passes through the progression
    for n in range(RATE * SECONDS):
        chord = CHORDS[(n // per_chord) % len(CHORDS)]
        t = n / RATE
        v = 0.0
        for note in chord:
            f = hz(note)
            v += math.sin(2 * math.pi * f * t) + 0.3 * math.sin(4 * math.pi * f * t)
        out.append(int(max(-1.0, min(1.0, v / 8)) * 32000))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != ARGC:
        print("usage: make_test_wav.py OUT.wav", file=sys.stderr)
        return 2
    path = Path(argv[1])
    data = samples()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(data)}h", *data))
    print(f"wrote {path} ({len(data)} frames, {RATE} Hz)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
