"""Synthetic two-deck transitions for the scorer's mutation tests (a fixture, never a fake of the scorer).

Each deck is built from parts the scorer must tell apart: a kick on every
beat with an accented beat 1, a bass note under each bar, off-beat hats, a
vocal-like harmonic tone over phrases, and a -60 dBFS noise floor (real
recordings are never digitally silent between hits). The mixer is applied the
way a bass-swap blend does it: the incoming fader opens (equal-power curve) over the first half,
the outgoing one closes over the second, the
incoming low band held at -24 dB until the swap bar, then the swap made as a
5 ms ramp centered on the downbeat (the Mixxx one-buffer ramp, scaled down so
the step lands on its bar).

``Mutation`` names one defect to inject; :func:`render` returns the mix, the
two deck outputs (as heard, like ``--decks-out``) and the matching
:class:`~apps.transition_score.scorer.RenderContext`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal

from apps.transition_score.scorer import RenderContext, Step

SR = 48000
BPM = 120.0
BEAT_S = 60.0 / BPM
BAR_S = 4 * BEAT_S
CONTEXT_BARS = 4
OVERLAP_BARS = 16
TOTAL_BARS = 2 * CONTEXT_BARS + OVERLAP_BARS
LENGTH_S = TOTAL_BARS * BAR_S
OVERLAP = (CONTEXT_BARS * BAR_S, (CONTEXT_BARS + OVERLAP_BARS) * BAR_S)
SWAP_S = OVERLAP[0] + OVERLAP_BARS / 2 * BAR_S
#: Low band held this far down before the swap (incoming) and after it (outgoing).
HELD_LOW = 10 ** (-24 / 20)
RAMP_S = 0.005
MASTER = 0.35
#: Hats live above 5 kHz, as a real hat does: white noise would put a hat in the kick's band.
_HAT_FILTER = signal.butter(4, 5000.0, btype="highpass", fs=SR, output="sos")


@dataclass(frozen=True)
class Mutation:
    """One injected defect; the default is the clean render."""

    in_shift_s: float = 0.0
    in_low_unheld: bool = False
    vocals_overlap: bool = False
    bump_lu: float = 0.0
    seam_click: bool = False
    clip_gain: float = 1.0
    step_late_s: float = 0.0
    gap_s: float = 0.0
    out_rolling_bass: bool = False
    #: Round 6: a bass-led outgoing section, a bass hit 45 ms before every grid beat, louder than the kick.
    out_bass_led: bool = False
    #: Round 10 (F3): the incoming lows killed (not held at -24 dB) until the swap, as a full EQ kill does.
    in_lows_killed: bool = False
    #: The plan itself asks for the swap at the late time (the render is faithful to a plan that is off the bar).
    plan_says_late: bool = False
    #: Round 14: the mix held ``dip_lu`` under the outgoing deck's solo level for ``dip_bars`` bars, centered on the swap
    #: (a loudness hole against the track being left).
    dip_lu: float = 0.0
    dip_bars: float = 0.0
    #: Round 14: a breakdown in the outgoing track itself, ``out_breakdown_lu`` down for 4 bars from 2 bars into the
    #: overlap, in its own audio (so in ``out_natural`` too): the track's structure, not a transition fault.
    out_breakdown_lu: float = 0.0
    #: Round 15: the maintainer's low handoff instead of the swap: the incoming LO parked at -8.7 dB (a third of a half turn)
    #: until 2 bars before the swap bar, then over 4 bars the incoming LO comes to flat while the outgoing LO goes to
    #: -8.7 dB, both partly present by design; the outgoing then leaves with its fader.
    maintainer_low_handoff: bool = False
    #: Round 15: both decks' lows killed for 4 bars around the swap (a low-end hole).
    low_hole: bool = False
    #: Round 23: outgoing/incoming low band killed through that deck's solo span
    #: (t < OVERLAP[0] / t >= OVERLAP[1]). Default False keeps the locked clean fixture.
    out_solo_low_killed: bool = False
    in_solo_low_killed: bool = False
    #: #10187: the outgoing's mid content (its vocal tone) eased ``out_mids_thin_lu`` down in its deck output over the
    #: first 2 bars of the overlap, linear in dB, then held: the mix thins under the outgoing at unity, whose own audio
    #: (out_natural) keeps it. Default 0 keeps the locked clean fixture.
    out_mids_thin_lu: float = 0.0


def _t(length_s: float = LENGTH_S) -> np.ndarray:
    return np.arange(int(length_s * SR)) / SR


def _drums(
    t: np.ndarray, kick_hz: float, bass_hz: float, shift_s: float, rng: np.random.Generator, beat_s: float = BEAT_S
) -> tuple[np.ndarray, np.ndarray]:
    """(low, high): kick plus bass, and hats, for a deck whose beat 1 sits at ``shift_s`` (mod one bar)."""
    low = np.zeros_like(t)
    high = np.zeros_like(t)
    beats = np.arange(-8, int(t[-1] / beat_s) + 8) * beat_s + shift_s
    for index, beat in enumerate(beats):
        start = round(beat * SR)
        if start >= len(t):
            break
        accent = 1.0 if index % 4 == 0 else 0.45
        n = int(0.18 * SR)
        k = np.arange(n) / SR
        sweep = kick_hz * (1 + 1.5 * np.exp(-k / 0.02))
        attack = np.minimum(k / 0.001, 1.0)  # a 1 ms attack, as a sampled kick has
        kick = accent * np.sin(2 * np.pi * np.cumsum(sweep) / SR) * np.exp(-k / 0.06) * attack
        lo, hi = max(0, start), min(len(t), start + n)
        if hi > lo:
            low[lo:hi] += kick[lo - start : hi - start]
        hat_at = round((beat + beat_s / 2) * SR)
        hn = int(0.03 * SR)
        hlo, hhi = max(0, hat_at), min(len(t), hat_at + hn)
        if hhi > hlo:
            noise = signal.sosfilt(_HAT_FILTER, rng.standard_normal(hhi - hlo))
            high[hlo:hhi] += 0.08 * noise * np.exp(-np.arange(hhi - hlo) / SR / 0.008)
    bass_phase = 2 * np.pi * bass_hz * (t - shift_s)
    # Side-chained, as dance bass is mixed: the bass ducks under each kick and recovers over ~120 ms,
    # so the kick's attack is the kick's, not the kick beating against the bass.
    since_beat = np.mod(t - shift_s, beat_s)
    duck = np.convolve(1.0 - 0.9 * np.exp(-since_beat / 0.06), np.ones(96) / 96, mode="same")  # 2 ms attack
    low += 0.25 * np.sin(bass_phase) * duck * (0.6 + 0.4 * np.cos(2 * np.pi * (t - shift_s) / (4 * beat_s)))
    return low, high


def _stabs(t: np.ndarray, hz: float, after_s: float, level: float) -> np.ndarray:
    """A rolling bass: one sharp low stab ``after_s`` past every beat, louder than the kick (round 1's outgoing track)."""
    out = np.zeros_like(t)
    n = int(0.08 * SR)
    k = np.arange(n) / SR
    stab = level * np.sin(2 * np.pi * hz * k) * np.exp(-k / 0.03) * np.minimum(k / 0.001, 1.0)
    for beat in np.arange(0.0, t[-1], BEAT_S):
        start = round((beat + after_s) * SR)
        stop = min(len(t), start + n)
        out[start:stop] += stab[: stop - start]
    return out


def _vocal(t: np.ndarray, base_hz: float, active: tuple[float, float]) -> np.ndarray:
    vib = 1 + 0.01 * np.sin(2 * np.pi * 5.5 * t)
    phase = 2 * np.pi * np.cumsum(base_hz * vib) / SR
    tone = sum((0.5 / h) * np.sin(h * phase) for h in range(1, 6))
    gate = ((t >= active[0]) & (t < active[1])).astype(float)
    return 0.12 * tone * np.convolve(gate, np.ones(480) / 480, mode="same")


def _ramp_step(t: np.ndarray, at_s: float, before: float, after: float) -> np.ndarray:
    x = np.clip((t - (at_s - RAMP_S / 2)) / RAMP_S, 0.0, 1.0)
    return before + (after - before) * (0.5 - 0.5 * np.cos(np.pi * x))


#: the maintainer's parked LO: a third of a half turn down from flat on the engine's EQ knob (0.5 flat, 0 = -26 dB).
MAINTAINER_PARKED_LO = 10 ** (-8.7 / 20)


def _maintainer_handoff(t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(outgoing, incoming) low gains: incoming parked at -8.7 dB, then a 4-bar crossover, linear in dB, centered on the
    swap bar, after which the outgoing low stays at -8.7 dB under its own fader fade."""
    x = np.clip((t - (SWAP_S - 2 * BAR_S)) / (4 * BAR_S), 0.0, 1.0)
    parked_db = 20 * np.log10(MAINTAINER_PARKED_LO)
    return 10 ** (parked_db * x / 20), 10 ** (parked_db * (1 - x) / 20)


def _faders(t: np.ndarray, maintainer_handoff: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """The incoming fader opens over the first half, the outgoing one closes over the second (both full at the swap).
    the maintainer's handoff (round 15): the incoming is up before his 4-bar low crossover, the outgoing leaves after it."""
    rise_end = SWAP_S - 2 * BAR_S if maintainer_handoff else SWAP_S
    fall_start = SWAP_S + 2 * BAR_S if maintainer_handoff else SWAP_S
    rise = np.clip((t - OVERLAP[0]) / (rise_end - OVERLAP[0]), 0.0, 1.0)
    fall = np.clip((t - fall_start) / (OVERLAP[1] - fall_start), 0.0, 1.0)
    return np.cos(fall * np.pi / 2), np.sin(rise * np.pi / 2)


def _level_mutation_gain(t: np.ndarray, mutation: Mutation) -> np.ndarray:
    """The shared gain the level mutations put on both decks: a loudness bump, clipping gain, a dropout."""
    gain = np.ones_like(t)
    if mutation.bump_lu:
        lo, hi = OVERLAP[0] + 2 * BAR_S, OVERLAP[1] - 2 * BAR_S
        gain = gain * (1 + (10 ** (mutation.bump_lu / 20) - 1) * np.clip(np.minimum(t - lo, hi - t) / 0.5, 0.0, 1.0))
    if mutation.clip_gain != 1.0:
        gain = gain * np.where((t >= OVERLAP[0]) & (t < OVERLAP[1]), mutation.clip_gain, 1.0)
    if mutation.gap_s:
        gain = gain * ~((t >= SWAP_S + BAR_S) & (t < SWAP_S + BAR_S + mutation.gap_s))
    return gain


def _dip_gain(t: np.ndarray, mix: np.ndarray, mutation: Mutation) -> np.ndarray:
    """The gain that holds the mix ``dip_lu`` under the outgoing solo's power for ``dip_bars`` bars (1.0 elsewhere):
    the mix's own 1 s power is divided out, so the hole is flat whatever the blend does under it."""
    if not mutation.dip_lu:
        return np.ones_like(t)
    solo = float(np.mean(mix[(t >= BAR_S) & (t < OVERLAP[0])] ** 2))
    cumulative = np.concatenate([[0.0], np.cumsum(mix**2)])
    index = np.arange(len(t))
    lo_i, hi_i = np.clip(index - SR // 2, 0, len(t)), np.clip(index + SR // 2, 0, len(t))
    power = np.maximum((cumulative[hi_i] - cumulative[lo_i]) / (hi_i - lo_i), 1e-12)
    held = np.sqrt(solo * 10 ** (-mutation.dip_lu / 10) / power)
    lo, hi = SWAP_S - mutation.dip_bars * BAR_S / 2, SWAP_S + mutation.dip_bars * BAR_S / 2
    inside = np.clip(np.minimum(t - lo, hi - t) / RAMP_S, 0.0, 1.0)  # 5 ms edges: the hole is its stated length
    return 1 + (held - 1) * inside


def render(mutation: Mutation | None = None, seed: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray, RenderContext]:
    """(mix, out_deck, in_deck, context), each deck stereo and as heard."""
    mutation = mutation or Mutation()
    rng = np.random.default_rng(seed)
    t = _t()
    out_low, out_high = _drums(t, 50.0, 41.2, 0.0, rng)
    in_low, in_high = _drums(t, 62.0, 55.0, mutation.in_shift_s, rng)
    if mutation.out_rolling_bass:
        out_low = out_low + _stabs(t, 70.0, BEAT_S / 4, 1.3)
    if mutation.out_bass_led:
        out_low = out_low + _stabs(t, 70.0, BEAT_S - 0.045, 1.5)
    if mutation.out_solo_low_killed:
        out_low = np.where(t < OVERLAP[0], 0.0, out_low)
    if mutation.in_solo_low_killed:
        in_low = np.where(t >= OVERLAP[1], 0.0, in_low)
    out_vocal_span = (0.0, 20.0) if mutation.vocals_overlap else (0.0, 16.0)
    in_vocal_span = (12.0, LENGTH_S) if mutation.vocals_overlap else (18.0, LENGTH_S)
    out_vocal, in_vocal = _vocal(t, 220.0, out_vocal_span), _vocal(t, 330.0, in_vocal_span)
    if mutation.out_breakdown_lu:
        lo, hi = OVERLAP[0] + 2 * BAR_S, OVERLAP[0] + 6 * BAR_S
        inside = np.clip(np.minimum(t - lo, hi - t) / RAMP_S, 0.0, 1.0)
        breakdown = 1 + (10 ** (-mutation.out_breakdown_lu / 20) - 1) * inside
        out_low, out_high, out_vocal = out_low * breakdown, out_high * breakdown, out_vocal * breakdown
    out_vocal_heard = out_vocal
    if mutation.out_mids_thin_lu:
        fade = np.clip((t - OVERLAP[0]) / (2 * BAR_S), 0.0, 1.0)
        out_vocal_heard = out_vocal * 10 ** (-mutation.out_mids_thin_lu * fade / 20)
    swap = SWAP_S + mutation.step_late_s
    out_low_gain = _ramp_step(t, swap, 1.0, HELD_LOW)
    held = 0.0 if mutation.in_lows_killed else HELD_LOW
    in_low_gain = np.ones_like(t) if mutation.in_low_unheld else _ramp_step(t, swap, held, 1.0)
    if mutation.maintainer_low_handoff:
        out_low_gain, in_low_gain = _maintainer_handoff(t)
    if mutation.low_hole:
        hole = (t >= SWAP_S - 2 * BAR_S) & (t < SWAP_S + 2 * BAR_S)
        out_low_gain, in_low_gain = np.where(hole, 0.0, out_low_gain), np.where(hole, 0.0, in_low_gain)
    out_fader, in_fader = _faders(t, mutation.maintainer_low_handoff)
    floor_out, floor_in = 0.001 * rng.standard_normal(len(t)), 0.001 * rng.standard_normal(len(t))
    out_source = MASTER * (out_low + out_high + out_vocal + floor_out)  # the outgoing alone: no fader, no EQ
    out_deck = MASTER * out_fader * (out_low * out_low_gain + out_high + out_vocal_heard + floor_out)
    in_deck = MASTER * in_fader * (in_low * in_low_gain + in_high + in_vocal + floor_in)
    gain = _level_mutation_gain(t, mutation)
    out_deck, in_deck = out_deck * gain, in_deck * gain
    dip = _dip_gain(t, out_deck + in_deck, mutation)
    out_deck, in_deck = out_deck * dip, in_deck * dip
    mix = out_deck + in_deck
    seams: tuple[float, ...] = ()
    if mutation.seam_click:
        seam = OVERLAP[0] + 2 * BAR_S
        seams = (seam,)
        start = int(seam * SR)
        mix[start:] += 0.3 * np.exp(-np.arange(len(mix) - start) / SR / 0.002)
    planned = SWAP_S + (mutation.step_late_s if mutation.plan_says_late else 0.0)
    stereo = [np.stack([x, x], axis=1).astype(np.float32) for x in (mix, out_deck, in_deck)]
    downbeats = tuple(i * BAR_S for i in range(TOTAL_BARS + 1))
    ctx = RenderContext(
        sample_rate=SR,
        beat_s=BEAT_S,
        overlap_s=OVERLAP,
        out_downbeats_s=downbeats,
        steps=() if mutation.maintainer_low_handoff else (Step(planned, "out"), Step(planned, "in")),  # his handoff ramps
        seams_s=seams,
        tempo_ratios=(1.0,),
        intent="smooth",
        shape=None,
        out_natural=np.stack([out_source, out_source], axis=1).astype(np.float32),
        vocal_out=(MASTER * out_fader * out_vocal_heard).astype(np.float32),
        vocal_in=(MASTER * in_fader * in_vocal).astype(np.float32),
    )
    return stereo[0], stereo[1], stereo[2], ctx


def track_audio(bpm: float, bars: int, kick_hz: float, bass_hz: float, seed: int) -> np.ndarray:
    """A whole invented track on a steady grid from t = 0 (stereo float32): kick, side-chained bass, hats."""
    rng = np.random.default_rng(seed)
    t = _t(bars * 240.0 / bpm)
    low, high = _drums(t, kick_hz, bass_hz, 0.0, rng, beat_s=60.0 / bpm)
    mono = MASTER * (low + high + 0.001 * rng.standard_normal(len(t)))
    return np.stack([mono, mono], axis=1).astype(np.float32)
