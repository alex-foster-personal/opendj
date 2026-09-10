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

TWO HYPOTHESES, TWO UNANIMOUS TESTS, AND UNKNOWN IN BETWEEN. Failing to
establish AUDIBLE is not evidence of silence. Music has dynamics and the mic
has noise, so one quiet ON window turns 6/6 into 5/6, and calling that
NOT AUDIBLE would send an outage investigation the wrong way while five pairs
showed signal. NOT AUDIBLE is therefore its own claim with its own unanimous
test in the other direction (OFF at least as loud as ON in every pair), and
anything mixed returns UNKNOWN.

IT PUTS THE DECK BACK. Run against a live app, so the transport state is read
before the first toggle and restored in a `finally`.

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
        ["pmset", "-g", "assertions"], capture_output=True, text=True, timeout=15, check=False
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
                ["ps", "-p", pid, "-o", "comm="], capture_output=True, text=True, check=False
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
        check=False,
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
            capture_output=True, text=True, timeout=seconds + 30, check=False,
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


def _deck_transport(origin: str, deck: int) -> dict:
    """The ENGINE-PUBLISHED transport state for one deck. Never the command reply.

    `playing` is what the engine says it is doing; `trust` compares the
    presentation clock's desired revision against its presented one, so a
    schedule the listener has not been handed yet reads as untrusted rather
    than as applied.
    """
    mirror = _get(origin, "/api/v1/state/ui-mirror")
    published = mirror.get("decks", {}).get(str(deck))
    if published is None:
        raise Unmeasurable(
            f"ui-mirror publishes no deck {deck}; decks={sorted(mirror.get('decks', {}))}"
        )
    return {
        "playing": published.get("playing"),
        "trust": published.get("presentation_clock", {}).get("trust"),
    }


# How long a confirmed toggle may take to appear in the published state. Two
# mirror publishes (1000ms each) plus the schedule ack, with margin.
TOGGLE_CONFIRM_TIMEOUT_S = 6.0
TOGGLE_POLL_S = 0.25


def transport_reached(published: dict, playing: bool) -> bool:
    """Whether the ENGINE-PUBLISHED transport shows the requested state, applied.

    Both halves are required. `playing` alone can be true while the schedule
    the listener is actually being handed is a revision behind, which is the
    state a probe must not sample in: `trust` is `trusted` only when the
    presentation clock's desired and presented revisions agree.

    A pure decision so it can be exercised without a running engine.
    """
    return published.get("playing") is playing and published.get("trust") == "trusted"


def _set_deck(origin: str, deck: int, playing: bool) -> None:
    """Order a transport change and CONFIRM it through the presented transport.

    A 200 with `status: succeeded` says the bus accepted the order, not that
    the deck moved. The Thu 10 Sep 2026 outage is exactly that case: the bus
    reported success while `playing` stayed false. Taking the step status as
    proof would let both halves of an A/B pair be sampled in the SAME transport
    state, after which room noise alone decides the verdict -- a check that
    returns AUDIBLE or NOT AUDIBLE for a measurement that never happened.

    So the target state must be observed in the engine-published mirror, with a
    trusted presentation clock, or the measurement is refused.
    """
    import time

    result = _post(
        origin,
        "/api/v1/commands",
        {"single": {"type": "play", "deck": deck, "playing": playing}},
    )
    steps = result.get("steps", [])
    if not steps or steps[0].get("status") != "succeeded":
        raise Unmeasurable(f"command bus refused play={playing} on deck {deck}: {result}")

    deadline = time.monotonic() + TOGGLE_CONFIRM_TIMEOUT_S
    last: dict = {}
    while time.monotonic() < deadline:
        last = _deck_transport(origin, deck)
        if transport_reached(last, playing):
            return
        time.sleep(TOGGLE_POLL_S)
    raise Unmeasurable(
        f"deck {deck} did not reach playing={playing} through the presented transport "
        f"within {TOGGLE_CONFIRM_TIMEOUT_S}s (bus said succeeded; published state {last!r}). "
        "The A/B pair would have been sampled in one transport state, so no acoustic "
        "verdict is possible."
    )


def acoustic_sign_test(
    origin: str, deck: int, cycles: int, sample_s: float, settle_s: float, tone_hz: int | None
) -> dict:
    """Paired A/B toggle. Immune to mic AGC drift in a way a threshold is not.

    RESTORES THE DECK. This runs against a LIVE app, so a probe that leaves a
    parked deck playing (or stops one that was) has changed the thing it was
    sent to observe. The state is read before the first toggle and put back in
    a `finally`, so a mic failure mid-run does not leave the deck stopped.
    """
    import time

    entry = _deck_transport(origin, deck)
    was_playing = entry["playing"]

    pairs = []
    try:
        for _ in range(cycles):
            _set_deck(origin, deck, False)
            time.sleep(settle_s)
            off = capture_level(sample_s, tone_hz)
            _set_deck(origin, deck, True)
            time.sleep(settle_s)
            on = capture_level(sample_s, tone_hz)

            pick = (lambda lv: lv.inband_db) if tone_hz else (lambda lv: lv.broadband_db)
            pairs.append({"off_db": pick(off), "on_db": pick(on), "delta_db": pick(on) - pick(off)})
    finally:
        # Best effort by necessity: this runs while an exception may already be
        # propagating, and a restore failure must not replace the original
        # cause. It is REPORTED on stderr rather than swallowed, so a deck left
        # in the wrong state is never silent.
        if was_playing is not None:
            try:
                _set_deck(origin, deck, bool(was_playing))
            except Exception as restore_exc:  # surface, never swallow
                print(
                    f"WARNING: could not restore deck {deck} to playing={was_playing}: "
                    f"{type(restore_exc).__name__}: {restore_exc}",
                    file=sys.stderr,
                )

    wins = sum(1 for p in pairs if p["delta_db"] > 0)
    return {
        "pairs": pairs,
        "cycles": cycles,
        "on_louder_in": wins,
        "off_louder_in": cycles - wins,
        "deck_was_playing": was_playing,
        # Named for the hypothesis each supports, and populated ONLY when its
        # own sign test is unanimous. A mixed result supports neither.
        "p_audible": 2.0**-cycles if wins == cycles else None,
        "p_silent": 2.0**-cycles if wins == 0 else None,
        "median_delta_db": sorted(p["delta_db"] for p in pairs)[cycles // 2],
    }


# ---------------------------------------------------------------- verdict

# UNKNOWN shares its exit code with "the layer did not measure", because both
# mean the same thing to a caller: this run did not answer the question.
VERDICT_EXIT = {"AUDIBLE": 0, "NOT AUDIBLE": 1, "UNKNOWN": 3}


def acoustic_verdict(acoustic: dict) -> tuple[str, str]:
    """Turn one acoustic result into a verdict and the line explaining it.

    TWO CLAIMS, EACH NEEDING ITS OWN UNANIMOUS SIGN TEST.

    `on_louder_in != cycles` establishes only that AUDIBLE was not shown at the
    stated significance. It does NOT establish that output is absent, and
    printing NOT AUDIBLE for it would be verifying the absence of a good thing
    from a check that never asked the opposite question. With real music
    dynamics or mic noise a single quieter ON window turns 6/6 into 5/6, and
    that result would send an outage hunt down the wrong path while five pairs
    showed signal.

    Silence predicts something specific: OFF at least as loud as ON in EVERY
    pair. That is a sign test in the other direction with the same p = 2**-N,
    and it is what NOT AUDIBLE is required to observe. Everything else is
    UNKNOWN.

    Pure, so the boundaries can be exercised in both directions without a
    running engine or a microphone.
    """
    cycles, wins = acoustic["cycles"], acoustic["on_louder_in"]
    median = acoustic["median_delta_db"]
    if wins == cycles:
        return "AUDIBLE", (
            f"on louder in {cycles}/{cycles} cycles, p={acoustic['p_audible']:.3f}, "
            f"median {median:+.1f} dB"
        )
    if wins == 0:
        return "NOT AUDIBLE", (
            f"off at least as loud as on in {cycles}/{cycles} cycles, "
            f"p={acoustic['p_silent']:.3f}, median {median:+.1f} dB"
        )
    return "UNKNOWN", (
        f"inconclusive: on louder in {wins}/{cycles} cycles, unanimous in neither "
        f"direction (median delta {median:+.1f} dB). Neither audible nor silent is "
        f"established; re-run with more --cycles or a --tone-hz source"
    )


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
        except Exception as exc:  # surface, never swallow
            report[name] = {"UNKNOWN": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(report, indent=2))

    # Verdict comes ONLY from the acoustic layer. The other two cannot answer
    # the question and must never be allowed to imply an answer.
    acoustic = report.get("acoustic")
    if not isinstance(acoustic, dict) or "UNKNOWN" in acoustic:
        print("\nVERDICT: UNKNOWN - the acoustic layer did not measure.", file=sys.stderr)
        return 3
    verdict, line = acoustic_verdict(acoustic)
    # Only the affirmative answer goes to stdout. Everything else is a state a
    # caller must not pipe onward as a result.
    stream = sys.stdout if verdict == "AUDIBLE" else sys.stderr
    print(f"\nVERDICT: {verdict} ({line})", file=stream)
    return VERDICT_EXIT[verdict]


if __name__ == "__main__":
    raise SystemExit(main())
