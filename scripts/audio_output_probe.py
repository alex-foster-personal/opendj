#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Ground-truth probe: is Open DJ actually emitting sound?

WHY THIS EXISTS
---------------
The app's own `master.rms` cannot answer that question. The meter is a
dead-end AnalyserNode hanging off `_masterGain`
(audio-engine.svelte.ts:678-679), which is upstream of
`_masterMuteGain -> ctx.destination` (:689-690). Every failure downstream of
that tap -- dead output device, re-routed destination, a WebKit GPU process
that lost its stream -- is invisible to it.

Measured live on Thu 10 Sep 2026 during a real outage: `master.rms` read 0.24
and varying, `context_state` read "running", and the room was silent.

So this probe never asks the app whether it is working. It measures the three
layers independently and refuses to return a verdict when it cannot measure.

LAYERS
------
1. APP    -- what the engine claims (ui-mirror). Upstream of the failure.
              Necessary context, never proof.
2. OS     -- which processes CoreAudio reports holding an output stream.
              Proves a stream exists, NOT that it carries signal. Cannot be
              attributed to Open DJ specifically: several apps share
              com.apple.WebKit.GPU.
3. ACOUSTIC -- a paired A/B toggle measured at the microphone. The only layer
              that observes the actual output.

WHY A PAIRED TOGGLE, NOT A THRESHOLD
------------------------------------
The built-in mic applies AGC. Measured floor drift was 12 dB between samples
minutes apart, which is larger than the signal being looked for. Any absolute
dB threshold is therefore unsound. Instead each cycle pauses the deck, samples,
plays the deck, samples, and compares the PAIR. Drift affects both halves of a
pair equally.

The verdict is a sign test over N cycles: if ON exceeds OFF in every cycle, the
probability under the null hypothesis of no audio is 2**-N. Six cycles gives
p = 0.016. This is robust to drift in a way a threshold is not.

WHY BROADBAND RMS IS NOT USED FOR TONES
---------------------------------------
Measured Thu 10 Sep 2026: broadband read -28.1 dB with a tone playing and
-28.0 dB with silence. Zero discrimination. Narrowband detection at the tone
frequency gave 22 dB of margin on the same hardware. `--tone` mode therefore
bandpasses; music mode cannot and relies on the sign test instead.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LOCK_PATH = Path.home() / "Library/Application Support/com.opendj.desktop/.engine.lock"
MIC_DEVICE = ":1"  # avfoundation audio index; see --list-devices


class Unmeasurable(RuntimeError):
    """Raised when a layer cannot be measured. Never rendered as a verdict."""


# ---------------------------------------------------------------- helpers


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise Unmeasurable(f"{binary} not on PATH; cannot measure the acoustic layer")
    return path


def _origin_from_lock() -> str:
    """Resolve the engine origin from the singleton lock. Never guess a port."""
    if not LOCK_PATH.is_file():
        raise Unmeasurable(f"no engine lock at {LOCK_PATH}; is the app running?")
    lock = json.loads(LOCK_PATH.read_text())
    host, port = lock.get("host"), lock.get("port")
    if not host or not port:
        raise Unmeasurable(f"engine lock lacks host/port: {lock!r}")
    return f"http://{host}:{port}"


def _get(origin: str, path: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(f"{origin}{path}", timeout=timeout) as fh:
        return json.loads(fh.read())


def _post(origin: str, path: str, body: dict, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        f"{origin}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------- layer 1


def app_claim(origin: str) -> dict:
    """What the engine says. Upstream of the failure -- context, not proof."""
    mirror = _get(origin, "/api/v1/state/ui-mirror")
    decks = mirror.get("decks", {})
    return {
        "context_state": mirror.get("context_state"),
        "master_rms": mirror.get("master", {}).get("rms"),
        "master_muted": mirror.get("master", {}).get("muted"),
        "playing_decks": [d for d, v in decks.items() if v.get("playing")],
        "xruns": mirror.get("xrun_sentinel", {}).get("xruns"),
        "parked": mirror.get("xrun_sentinel", {}).get("parked"),
    }


# ---------------------------------------------------------------- layer 2


def os_audio_out_holders() -> list[str]:
    """Processes CoreAudio reports holding an output stream, resolved to names.

    A held stream does NOT mean signal is flowing through it. During the
    Thu 10 Sep 2026 outage a WebKit GPU process held one the entire time.
    """
    out = subprocess.run(
        ["pmset", "-g", "assertions"], capture_output=True, text=True, timeout=15
    )
    if out.returncode != 0:
        raise Unmeasurable(f"pmset failed rc={out.returncode}: {out.stderr.strip()}")

    holders, pid = [], None
    for line in out.stdout.splitlines():
        found = re.search(r"Created for PID:\s*(\d+)", line)
        if found:
            pid = found.group(1)
        elif "Resources:" in line and "audio-out" in line and pid:
            name = subprocess.run(
                ["ps", "-p", pid, "-o", "comm="], capture_output=True, text=True
            ).stdout.strip()
            holders.append(f"{pid}({Path(name).name or 'dead'})")
            pid = None
    return sorted(set(holders))


# ---------------------------------------------------------------- layer 3


@dataclass(frozen=True)
class Level:
    broadband_db: float
    inband_db: float | None


def _ffmpeg_mean_db(wav: Path, audio_filter: str) -> float:
    """Parse volumedetect mean_volume. Raises rather than returning a bogus 0."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(wav), "-af", audio_filter, "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # volumedetect prints at info level on stderr. Suppressing stderr here is
    # what silently produced empty readings during development -- an empty
    # string then parses as "no signal", which is indistinguishable from a
    # broken measurement. Hence: parse explicitly, and fail loudly.
    matches = re.findall(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    if not matches:
        raise Unmeasurable(
            f"volumedetect produced no mean_volume for filter {audio_filter!r}; "
            f"ffmpeg rc={proc.returncode}"
        )
    return float(matches[-1])


def capture_level(seconds: float, tone_hz: int | None) -> Level:
    _require("ffmpeg")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "cap.wav"
        rec = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
             "-i", MIC_DEVICE, "-t", str(seconds), "-ac", "1", "-ar", "44100",
             "-y", str(wav)],
            capture_output=True, text=True, timeout=seconds + 30,
        )
        if not wav.exists() or wav.stat().st_size == 0:
            raise Unmeasurable(
                "microphone capture produced no data. Grant the terminal "
                f"Microphone permission in System Settings. ffmpeg: {rec.stderr.strip()[:200]}"
            )
        broadband = _ffmpeg_mean_db(wav, "volumedetect")
        inband = (
            _ffmpeg_mean_db(wav, f"bandpass=f={tone_hz}:width_type=h:w=12,volumedetect")
            if tone_hz
            else None
        )
        return Level(broadband_db=broadband, inband_db=inband)


def _set_deck(origin: str, deck: int, playing: bool) -> None:
    result = _post(
        origin,
        "/api/v1/commands",
        {"single": {"type": "play", "deck": deck, "playing": playing}},
    )
    steps = result.get("steps", [])
    if not steps or steps[0].get("status") != "succeeded":
        raise Unmeasurable(f"command bus refused play={playing} on deck {deck}: {result}")


def acoustic_sign_test(
    origin: str, deck: int, cycles: int, sample_s: float, settle_s: float, tone_hz: int | None
) -> dict:
    """Paired A/B toggle. Immune to mic AGC drift in a way a threshold is not."""
    import time

    pairs = []
    for _ in range(cycles):
        _set_deck(origin, deck, False)
        time.sleep(settle_s)
        off = capture_level(sample_s, tone_hz)
        _set_deck(origin, deck, True)
        time.sleep(settle_s)
        on = capture_level(sample_s, tone_hz)

        pick = (lambda lv: lv.inband_db) if tone_hz else (lambda lv: lv.broadband_db)
        pairs.append({"off_db": pick(off), "on_db": pick(on), "delta_db": pick(on) - pick(off)})

    wins = sum(1 for p in pairs if p["delta_db"] > 0)
    return {
        "pairs": pairs,
        "cycles": cycles,
        "on_louder_in": wins,
        "p_value": 2.0**-cycles if wins == cycles else None,
        "median_delta_db": sorted(p["delta_db"] for p in pairs)[cycles // 2],
    }


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deck", type=int, default=1, help="deck to toggle for the acoustic test")
    ap.add_argument("--cycles", type=int, default=6, help="A/B cycles; p = 2**-cycles")
    ap.add_argument("--sample-seconds", type=float, default=2.5)
    ap.add_argument("--settle-seconds", type=float, default=1.2)
    ap.add_argument("--tone-hz", type=int, default=None,
                    help="narrowband detection at this frequency (for tone sources)")
    ap.add_argument("--layers", default="app,os,acoustic",
                    help="comma-separated subset; acoustic makes noise and moves the deck")
    args = ap.parse_args()
    wanted = {s.strip() for s in args.layers.split(",")}

    report: dict = {}
    origin = None
    try:
        origin = _origin_from_lock()
        report["origin"] = origin
    except Unmeasurable as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    for name, fn in (
        ("app", lambda: app_claim(origin)),
        ("os", os_audio_out_holders),
        ("acoustic", lambda: acoustic_sign_test(
            origin, args.deck, args.cycles, args.sample_seconds,
            args.settle_seconds, args.tone_hz)),
    ):
        if name not in wanted:
            continue
        try:
            report[name] = fn()
        except Unmeasurable as exc:
            report[name] = {"UNKNOWN": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surface, never swallow
            report[name] = {"UNKNOWN": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(report, indent=2))

    # Verdict comes ONLY from the acoustic layer. The other two cannot answer
    # the question and must never be allowed to imply an answer.
    acoustic = report.get("acoustic")
    if not isinstance(acoustic, dict) or "UNKNOWN" in acoustic:
        print("\nVERDICT: UNKNOWN - the acoustic layer did not measure.", file=sys.stderr)
        return 3
    if acoustic["on_louder_in"] == acoustic["cycles"]:
        print(f"\nVERDICT: AUDIBLE (on louder in {acoustic['cycles']}/{acoustic['cycles']} "
              f"cycles, p={acoustic['p_value']:.3f}, median +{acoustic['median_delta_db']:.1f} dB)")
        return 0
    print(f"\nVERDICT: NOT AUDIBLE (on louder in only "
          f"{acoustic['on_louder_in']}/{acoustic['cycles']} cycles; "
          f"median delta {acoustic['median_delta_db']:+.1f} dB)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
