"""Build one blind calibration session from real try-er renders (TRYER-22).

Usage:
  python -m scripts.bench.tryer_calibration.build_manifest \\
      --live-data-dir <app data dir, read only> --render-dir <scratch dir> --session <name> \\
      [--pairs <fixture>] [--only p01,p11] [--profiles <snapshot dir>]

The live data dir is only READ: profiles come from the running engine (GET), or
with --profiles from saved snapshots (cached_source.py; D10, no app needed),
audio paths and loudness from its state.db opened read-only. Renders go to
a builder-owned child of ``--render-dir`` and that child is deleted after each
pair; any error other than SKIPPABLE_PAIR_ERRORS aborts the build.

Selection rule ``taste-v3`` (``SELECTION_RULE``, recorded in manifest and key):
the session decides TASTE, never by M and never filtered by veto (vetoes and M
are recorded in the hidden key). Style family and cue placement are crossed so
the fit can separate them:
  * factorial: (family A, exit X), (family B, exit X), (family A, exit Y), the
    first such triple in plan_sha256 order (never the try-er's order, which is
    by M, nor its ids, which are numbered in that order), preferring A and B from the core
    lanes (blend, crossfade, filter);
  * fallback (reason recorded per pair): the taste-v1 walk, new family first,
    then new (family, exit bar);
  * plus a low anchor: the first variant with the incoming deck 40 ms late,
    re-mixed from its per-deck outputs after proving they sum to the mix.

Played clips (spec 10 decision 6: 4 bars of context scored, 2 bars played):
each clip is trimmed to the plan's span plus PLAYED_CONTEXT_BARS on each side,
computed from the render layout; the anchor is cut on its source's window.

Output, under ``scripts/bench/tryer_calibration/sessions/<name>/`` (gitignored):
``manifest.json`` (what the page sees: pairs, opaque clip ids, urls),
``key.json`` (hidden: role, family, style, cues, vetoes, M, features) and
``clips/<cid>.flac`` (lossless) with random names, and each clip's per-deck slices at
``clips/<cid>/decks/deck<N>.flac`` (paths in the key's ``decks``) for the open review page's two-deck lanes.

Requirements:
  ✔︎ ✅ 🎯 taste-v3 factorial selection is a pure function of a TryResult and ignores M and vetoes, order included
  ✔︎ ✅ 🎯 played clips are the plan span plus 2 bars each side, anchor on the same window
  ✔︎ ✅ 🎯 layer-2 features only: hygiene factors never enter the key's features
  ✔︎ ✅ 🎯 the anchor is refused unless the deck sum reproduces the mix
  ✔︎ ✅ 🎯 rated clips are bit-exact int16 slices of the 16-bit render; the anchor is never clipped
      (one constant gain, recorded as anchor_gain_db, when it would pass full scale)
  ✔︎ ✅ never writes under the live data dir
Acceptance:
  [if] no family x exit factorial exists [then] the v1 walk is used and the reason recorded per pair
  [if] fewer than 2 candidates differ in family or exit bar [then] the pair is skipped, reason recorded
  [if] the deck sum differs from the mix by more than ANCHOR_MAX_RESIDUAL_DB [then ⛔️] AnchorError
  [if] free disk falls below MIN_FREE_GB [then ⛔️] stop before the next pair
  [if] the session directory already exists [then ⛔️] refuse, never overwrite its key
  [if] zero pairs survive [then ⛔️] exit 1 and no manifest is written
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import secrets
import shutil
import sqlite3
import sys
import time
import uuid
import wave
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from apps.autodj.http import RENDER_SAMPLE_RATE, RenderPlanError, TrackProfile, build_service
from apps.opendj_cli.dj_service import DjError
from apps.transition_score.contract import CORE_FACTORS, SCORER_VERSION, Candidate, TryResult
from apps.transition_score.http import AudioUnavailable, EngineTrackSource, PairNotTryable, ProfileReader, try_pair
from scripts.bench.tryer_calibration.cached_source import CachedProfiles
from scripts.bench.tryer_calibration.pairs import FIXTURE_PATH, CalibrationPair, load_pairs_verified

_AUTODJ = build_service()

#: A pair is skipped (reason recorded) only when THIS pair cannot be tried: a track without audio or analysis, no
#: proposal, an unrenderable plan. A renderer or engine failure would hit every pair, so it aborts the build instead
#: of quietly publishing a partial session.
SKIPPABLE_PAIR_ERRORS: tuple[type[Exception], ...] = (PairNotTryable, RenderPlanError)
#: DjError codes that are about this pair's tracks; any other DjError (an engine or HTTP failure) aborts.
SKIPPABLE_DJ_CODES: frozenset[str] = frozenset({"not_found"})
SESSIONS_ROOT: Path = Path(__file__).parent / "sessions"
BENCH_ROOT: Path = Path(__file__).resolve().parents[1]


class CFG:
    """Session settings, all in one place."""

    TRY_N: int = 24
    TASTE_VARIANTS: int = 3
    PLAYED_CONTEXT_BARS: int = 2
    CORE_LANES: tuple[str, ...] = ("blend", "crossfade", "filter")
    MIN_FREE_GB: float = 13.0
    CONTEXT_BARS: int = 4
    ANCHOR_DELAY_MS: float = 40.0
    ANCHOR_MAX_RESIDUAL_DB: float = -40.0
    PHRASE_BARS: int = 8
    #: Layer-2 (taste) factor names the scorer may emit (file 15 section 2). Anything
    #: else in ``Candidate.factors`` is hygiene and is never fitted against taste.
    LAYER2_FACTORS: tuple[str, ...] = (
        "mix_point_placement",
        "key_relation",
        "energy_step",
        "overlap_length",
        "timbre_distance",
        "chroma_distance",
        "variety",
    )


class ClipError(RuntimeError):
    """A clip cannot be written as an exact copy of its render."""


class AnchorError(RuntimeError):
    """The low anchor cannot be derived faithfully from this render."""


# -----------------------------------------------------------------------------
# selection (pure)
# -----------------------------------------------------------------------------
SELECTION_RULE = (
    "taste-v3: factorial (family A, exit X), (family B, exit X), (family A, exit Y) in plan_sha256 order, core lanes "
    "preferred; else taste-v1 walk with the reason recorded; M and vetoes ignored for selection and recorded; "
    "anchor = first variant with incoming +40 ms; played clip = plan span + 2 bars each side; plus the first "
    "lull_then_drop candidate in plan_sha256 order as role lull when the pair renders one (D13)"
)


#: Style families: the taste choice is the KIND of move, not its exact preset.
STYLE_FAMILY: dict[str, str] = {
    "bass_swap_blend": "blend",
    "long_blend": "blend",
    "melt": "blend",
    "wave": "blend",
    "crossfade": "crossfade",
    "filter_sweep": "filter",
    "rise": "filter",
    "cut": "cut",
    "drop_swap": "cut",
    "echo_out": "fx",
    "stem_swap": "stem",
    "loop_extend": "loop",
    # D13: its own family, outside the core lanes, so it never enters the taste factorial; added as role "lull".
    "lull_then_drop": "lull",
}


def cue_bars(candidate: Candidate) -> tuple[int, int]:
    """(outgoing, incoming) window start bars from the plan; KeyError names the gap."""
    return (int(candidate.plan["out"]["window"]["start_bar"]), int(candidate.plan["in"]["window"]["start_bar"]))


def family_of(candidate: Candidate) -> str:
    if candidate.style not in STYLE_FAMILY:
        raise KeyError(f"style {candidate.style!r} has no family in STYLE_FAMILY")
    return STYLE_FAMILY[candidate.style]


def exit_bar(candidate: Candidate) -> int:
    """The outgoing bar the transition ends on: its cue placement on the outgoing track."""
    return cue_bars(candidate)[0] + candidate.bars


@dataclass(frozen=True)
class Selection:
    variants: list[Candidate]
    design: str  # "factorial" or "fallback"
    reason: str | None


def _factorial(candidates: tuple[Candidate, ...], lanes: tuple[str, ...] | None) -> list[Candidate] | None:
    def allowed(c: Candidate) -> bool:
        return lanes is None or family_of(c) in lanes

    for a in candidates:
        if not allowed(a):
            continue
        for b in candidates:
            if not allowed(b) or family_of(b) == family_of(a) or exit_bar(b) != exit_bar(a):
                continue
            for c in candidates:
                if family_of(c) == family_of(a) and exit_bar(c) != exit_bar(a):
                    return [a, b, c]
    return None


def _taste_v1_walk(candidates: tuple[Candidate, ...], count: int) -> list[Candidate]:
    chosen = [candidates[0]]
    for candidate in candidates[1:]:
        if len(chosen) < count and family_of(candidate) not in {family_of(c) for c in chosen}:
            chosen.append(candidate)
    for candidate in candidates[1:]:
        seen = {(family_of(c), exit_bar(c)) for c in chosen}
        if len(chosen) < count and candidate not in chosen and (family_of(candidate), exit_bar(candidate)) not in seen:
            chosen.append(candidate)
    return chosen


def _no_factorial_reason(candidates: tuple[Candidate, ...]) -> str:
    """Why no (A,X),(B,X),(A,Y) triple exists among these candidates."""
    families = {family_of(c) for c in candidates}
    families_at_exit: dict[int, set[str]] = {}
    for c in candidates:
        families_at_exit.setdefault(exit_bar(c), set()).add(family_of(c))
    if len(families) < 2:
        return f"only one style family ({sorted(families)[0]}) among {len(candidates)} candidates"
    if not any(len(f) > 1 for f in families_at_exit.values()):
        return "no exit bar is shared by two families"
    return "no family offers a second exit bar"


def select_taste_variants(result: TryResult, *, count: int = 3) -> Selection | str:
    """taste-v3: a family x exit-bar factorial where the candidates allow it, else the v1 walk with a reason.

    Candidates are taken in plan_sha256 order, a property of the plan alone: the try-er's own order (and its
    c1..cN ids) follow M, so a scorer change would otherwise change the clips the taste experiment hears.
    """
    rendered = tuple(
        sorted((c for c in result.candidates if c.refused is None), key=lambda c: c.plan_sha256)
    )  # a refused candidate has no audio to rate
    if not rendered:
        return f"try_pair returned no rendered candidates ({len(result.candidates)} refused)"
    # D13: lulls are role "lull", never a taste variant (factorial, any-family retry, or v1 walk).
    taste = tuple(c for c in rendered if c.style != "lull_then_drop")
    if not taste:
        return f"no taste contrast: all {len(rendered)} rendered candidates share family and exit bar"
    for lanes in (CFG.CORE_LANES, None):
        triple = _factorial(taste, lanes)
        if triple is not None:
            return Selection(
                triple, "factorial", None if lanes else "no factorial within the core lanes; used any family"
            )
    reason = _no_factorial_reason(taste)
    chosen = _taste_v1_walk(taste, count)
    if len(chosen) < 2:
        return f"no taste contrast: all {len(rendered)} rendered candidates share family and exit bar"
    return Selection(chosen, "fallback", reason)


def lull_candidate(result: TryResult, chosen: list[Candidate]) -> Candidate | None:
    """D13: the first rendered lull_then_drop candidate in plan_sha256 order not already chosen, so the maintainer can hear the
    intended lull-then-smash shape beside the taste variants; None when the pair renders none."""
    lulls = sorted(
        (c for c in result.candidates if c.refused is None and c.style == "lull_then_drop" and c not in chosen),
        key=lambda c: c.plan_sha256,
    )
    return lulls[0] if lulls else None


def layer2_features(candidate: Candidate) -> dict[str, float]:
    """Taste features only: style, overlap length, cue placement, layer-2 factors."""
    out_cue, in_cue = cue_bars(candidate)
    features: dict[str, float] = {
        f"family:{family_of(candidate)}": 1.0,
        "overlap_bars_log2": math.log2(candidate.bars),
        "exit_bar_phrases": exit_bar(candidate) / CFG.PHRASE_BARS,
        "out_cue_on_phrase": float((out_cue - 1) % CFG.PHRASE_BARS == 0),
        "in_cue_on_phrase": float((in_cue - 1) % CFG.PHRASE_BARS == 0),
    }
    for name in CFG.LAYER2_FACTORS:
        if name in candidate.factors:
            features[name] = float(candidate.factors[name])
    leaked = set(features) & set(CORE_FACTORS)
    if leaked:
        raise AssertionError(f"hygiene factors leaked into taste features: {sorted(leaked)}")
    return features


def style_vocabulary(features: list[dict[str, float]]) -> list[dict[str, float]]:
    """Give every candidate every family column (0.0 where it is another family)."""
    styles = sorted({k for f in features for k in f if k.startswith("family:")})
    return [{**{s: 0.0 for s in styles}, **f} for f in features]


# -----------------------------------------------------------------------------
# wav io (PCM16 / float32, the formats odj-audio writes)
# -----------------------------------------------------------------------------
def _riff_chunks(raw: bytes) -> dict[bytes, bytes]:
    """Top-level RIFF chunks by id (the first of each)."""
    chunks: dict[bytes, bytes] = {}
    pos = 12
    while pos + 8 <= len(raw):
        chunk, size = raw[pos : pos + 4], int.from_bytes(raw[pos + 4 : pos + 8], "little")
        chunks.setdefault(chunk, raw[pos + 8 : pos + 8 + size])
        pos += 8 + size + (size & 1)
    return chunks


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    raw = path.read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise AnchorError(f"{path} is not a RIFF/WAVE file")
    chunks = _riff_chunks(raw)
    fmt, data = chunks.get(b"fmt "), chunks.get(b"data")
    if fmt is None or data is None:
        raise AnchorError(f"{path}: missing fmt or data chunk")
    tag, channels, rate = (
        int.from_bytes(fmt[0:2], "little"),
        int.from_bytes(fmt[2:4], "little"),
        int.from_bytes(fmt[4:8], "little"),
    )
    bits = int.from_bytes(fmt[14:16], "little")
    if tag == 0xFFFE:
        tag = int.from_bytes(fmt[24:26], "little")
    if tag == 3 and bits == 32:
        samples = np.frombuffer(data, dtype="<f4").astype(np.float64)
    elif tag == 1 and bits == 16:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
    else:
        raise AnchorError(f"{path}: unsupported wav format tag={tag} bits={bits}")
    return samples.reshape(-1, channels), rate


def write_wav_pcm16(path: Path, samples: np.ndarray, rate: int) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).round().astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(samples.shape[1])
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


# -----------------------------------------------------------------------------
# anchor
# -----------------------------------------------------------------------------
#: Under each pair's render dir, where try_pair keeps every candidate's 16-bit mix and deck WAVs
#: (``keep_renders_in``: ``<cid>/mix.wav`` and ``<cid>/decks/``); the pair's render dir is always deleted.
KEPT_RENDERS = "kept"


def kept_render(candidate: Candidate) -> Path:
    """The candidate's kept 16-bit mix, or AnchorError when the try kept none (a refused candidate)."""
    if candidate.render_path is None:
        raise AnchorError(f"candidate {candidate.id} has no kept render (refused: {candidate.refused})")
    return Path(candidate.render_path)


def decks_dir_of(candidate: Candidate) -> Path:
    """Where try_pair kept this render's per-deck WAVs (beside the kept mix)."""
    render = kept_render(candidate)
    for option in (render.parent / "decks",):
        if (option / f"deck{candidate.plan['in']['deck']}.wav").is_file():
            return option
    raise AnchorError(f"no deck{candidate.plan['in']['deck']}.wav beside {render}; is --decks-out kept?")


def delayed_anchor(mix: np.ndarray, out_deck: np.ndarray, in_deck: np.ndarray, rate: int) -> np.ndarray:
    """Re-mix with the incoming deck ANCHOR_DELAY_MS late, after proving the deck sum is the mix."""
    if not (mix.shape == out_deck.shape == in_deck.shape):
        raise AnchorError(f"shape mismatch mix={mix.shape} out={out_deck.shape} in={in_deck.shape}")
    residual = mix - (out_deck + in_deck)
    level = 10 * math.log10((np.mean(residual**2) + 1e-20) / (np.mean(mix**2) + 1e-20))
    if level > CFG.ANCHOR_MAX_RESIDUAL_DB:
        raise AnchorError(f"deck sum differs from the mix by {level:.1f} dB (> {CFG.ANCHOR_MAX_RESIDUAL_DB} dB)")
    shift = round(CFG.ANCHOR_DELAY_MS * rate / 1000.0)
    late = np.zeros_like(in_deck)
    late[shift:] = in_deck[:-shift]
    return out_deck + late


def anchor_pcm16(anchor: np.ndarray) -> tuple[np.ndarray, float]:
    """The anchor as int16, scaled by ONE constant gain (returned in dB) when it would pass full scale; never clipped."""
    peak = float(np.max(np.abs(anchor), initial=0.0)) * PCM16_SCALE
    gain = min(1.0, PCM16_MAX / peak) if peak > 0 else 1.0
    return np.round(anchor * PCM16_SCALE * gain).astype(np.int16), 20.0 * math.log10(gain)


def render_anchor(top: Candidate, target: Path, window: tuple[float, float]) -> float:
    """Write the trimmed anchor; return the constant gain in dB applied to keep it unclipped (0.0 when none)."""
    decks = decks_dir_of(top)
    mix, rate = read_wav_pcm16(kept_render(top))
    out_deck, _ = read_wav_pcm16(decks / f"deck{top.plan['out']['deck']}.wav")
    in_deck, _ = read_wav_pcm16(decks / f"deck{top.plan['in']['deck']}.wav")
    mix_f, out_f, in_f = (a.astype(np.float64) / PCM16_SCALE for a in (mix, out_deck, in_deck))
    samples, gain_db = anchor_pcm16(trim(delayed_anchor(mix_f, out_f, in_f, rate), rate, window))
    write_flac_pcm16(target, samples, rate)
    return gain_db


# -----------------------------------------------------------------------------
# played window
# -----------------------------------------------------------------------------
def shared_start_bar(result: TryResult, out_p: TrackProfile, in_p: TrackProfile) -> float:
    """The outgoing bar every render of this try starts on (try_plans renders all plans from one start)."""
    return min(
        _AUTODJ.layout_of(_AUTODJ.parse_plan(c.plan), out_p, in_p, CFG.CONTEXT_BARS).start_out_bar
        for c in result.candidates
    )


def played_window(
    candidate: Candidate, out_p: TrackProfile, in_p: TrackProfile, start_bar: float, render_s: float
) -> tuple[float, float]:
    """(start, end) seconds in the render: the plan's span plus PLAYED_CONTEXT_BARS each side."""
    plan = _AUTODJ.parse_plan(candidate.plan)
    layout = _AUTODJ.layout_of(plan, out_p, in_p, CFG.CONTEXT_BARS, start_out_bar=start_bar)
    first = plan.out.start_bar + min(0.0, min((e.at_bar for e in plan.events), default=0.0))
    start = out_p.time_of_bar(first - CFG.PLAYED_CONTEXT_BARS) - layout.start_out_s
    if in_p.bpm is None:
        raise RenderPlanError(f"{in_p.stable_id}: no tempo, so its bars cannot be trimmed")
    end = render_s - trailing_trim_s(in_p.bpm, layout.in_ratio, layout.in_fold)
    if not 0.0 <= start < end <= render_s:
        raise RenderPlanError(
            f"{candidate.id}: played window {start:.2f}..{end:.2f} s is outside the {render_s:.2f} s render"
        )
    return start, end


def trailing_trim_s(in_bpm: float, in_ratio: float, in_fold: float) -> float:
    """Seconds of trailing context to drop: the unplayed plan-clock bars, each spanning ``in_fold`` incoming bars."""
    incoming_bar_render_s = 240.0 / in_bpm / in_ratio
    return (CFG.CONTEXT_BARS - CFG.PLAYED_CONTEXT_BARS) * in_fold * incoming_bar_render_s


def trim(samples: np.ndarray, rate: int, window: tuple[float, float]) -> np.ndarray:
    return samples[round(window[0] * rate) : round(window[1] * rate)]


# -----------------------------------------------------------------------------
# session assembly
# -----------------------------------------------------------------------------
PCM16_SCALE = 32768.0
PCM16_MAX = 32767.0


def read_wav_pcm16(path: Path) -> tuple[np.ndarray, int]:
    """A render's samples as int16, refusing anything that is not 16-bit PCM (no silent requantization)."""
    raw = path.read_bytes()
    samples, rate = read_wav(path)
    fmt = raw.find(b"fmt ")
    tag, bits = int.from_bytes(raw[fmt + 8 : fmt + 10], "little"), int.from_bytes(raw[fmt + 22 : fmt + 24], "little")
    if (tag, bits) != (1, 16):
        raise ClipError(
            f"{path}: render is format tag {tag}, {bits} bits; clips copy only 16-bit PCM renders bit-exactly"
        )
    return np.round(samples * PCM16_SCALE).astype(np.int16), rate


def write_flac_pcm16(path: Path, samples: np.ndarray, rate: int) -> None:
    """Lossless 16-bit FLAC of int16 samples: the file decodes to exactly these samples."""
    import soundfile  # the 'analysis' extra; try_pair already requires it

    if samples.dtype != np.int16:
        raise ClipError(f"{path}: refusing {samples.dtype} samples; clips are written from int16 only")
    soundfile.write(str(path), samples, rate, subtype="PCM_16", format="FLAC")


def _copy_clip(source: Path, clips: Path, window: tuple[float, float]) -> str:
    """A bit-exact slice of the 16-bit render."""
    cid = secrets.token_hex(6)
    samples, rate = read_wav_pcm16(source)
    write_flac_pcm16(clips / f"{cid}.flac", trim(samples, rate, window), rate)
    return cid


def _copy_decks(
    candidate: Candidate, clips: Path, cid: str, window: tuple[float, float], in_delay_ms: float = 0.0
) -> dict[str, str]:
    """Each deck's kept render, sliced to the clip's played window as lossless FLAC beside the clip
    (``clips/<cid>/decks/deck<N>.flac``), so a review page can draw the two-deck lanes the try-er panel draws.
    The anchor's incoming deck is delayed by the same ``in_delay_ms`` as its mix (zeros in front, never clipped)."""
    source = kept_render(candidate).parent / "decks"
    required: list[int] = []
    for n in (candidate.plan["out"]["deck"], candidate.plan["in"]["deck"]):
        if n not in required:
            required.append(n)
    for n in required:
        filename = f"deck{n}.wav"
        if not (source / filename).is_file():
            raise ClipError(f"candidate {candidate.id} missing {filename}")
    target = clips / cid / "decks"
    target.mkdir(parents=True)
    paths: dict[str, str] = {}
    for n in required:
        samples, rate = read_wav_pcm16(source / f"deck{n}.wav")
        if in_delay_ms and n == candidate.plan["in"]["deck"]:
            pad = np.zeros((round(in_delay_ms / 1000.0 * rate), *samples.shape[1:]), dtype=np.int16)
            samples = np.concatenate([pad, samples])[: len(samples)]
        write_flac_pcm16(target / f"deck{n}.flac", trim(samples, rate, window), rate)
        paths[str(n)] = f"clips/{cid}/decks/deck{n}.flac"
    return paths


def _key_entry(pair: CalibrationPair, role: str, candidate: Candidate, features: dict[str, float]) -> dict[str, Any]:
    return {
        "pair_id": pair.pair_id,
        "role": role,
        "candidate_id": candidate.id,
        "family": family_of(candidate),
        "style": candidate.style,
        "bars": candidate.bars,
        "cue_bars": list(cue_bars(candidate)),
        "exit_bar": exit_bar(candidate),
        "plan": candidate.plan,
        "plan_sha256": candidate.plan_sha256,
        "score": candidate.score,
        "vetoes": [v.to_json() for v in candidate.vetoes],
        "measured": candidate.measured.to_json(),
        "unknown": candidate.unknown,
        "metrics": candidate.metrics,
        "scorer_version": candidate.scorer_version,
        "factors": candidate.factors,
        "features": features,
    }


# -----------------------------------------------------------------------------
# read-only live source
# -----------------------------------------------------------------------------
@dataclass
class ReadOnlyLiveSource:
    """Profiles from the running engine (GET), or from saved snapshots when ``profiles_dir`` is given (D10: no app
    needed); audio path and loudness from the live state.db, read only."""

    live_data_dir: Path
    profiles_dir: Path | None = None

    def __post_init__(self) -> None:
        self._profiles: ProfileReader = (
            EngineTrackSource(self.live_data_dir, _AUTODJ)
            if self.profiles_dir is None
            else CachedProfiles(self.profiles_dir)
        )

    def profile(self, stable_id: str) -> TrackProfile:
        profile = self._profiles.profile(stable_id)
        if profile.lufs is not None:
            return profile
        db = self.live_data_dir / "state" / "state.db"
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT value FROM analysis_projection WHERE stable_id = ? AND field = 'loudness_lufs' AND status = 'ok'",
                (stable_id,),
            ).fetchone()
        if row is None:
            return profile  # the profile keeps its own lufs_reason; nothing is invented
        return replace(profile, lufs=float(row[0]), lufs_reason=None)

    def audio_path(self, stable_id: str) -> Path:
        from apps.stems.cli import resolve_audio_path  # opens state.db mode=ro

        try:
            return resolve_audio_path(self.live_data_dir, stable_id)
        except FileNotFoundError as error:  # this track's audio is not here: skip the pair, never the build
            raise AudioUnavailable(stable_id) from error


_SESSION_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def session_dir_for(name: str) -> Path:
    """The session's directory, strictly beneath SESSIONS_ROOT: a plain name, no separators or '..'."""
    if not _SESSION_NAME.match(name) or ".." in name:
        raise SystemExit(f"[ERROR] --session {name!r} must be a plain name (letters, digits, '.', '_', '-'; no '..')")
    target = (SESSIONS_ROOT / name).resolve()
    if target.parent != SESSIONS_ROOT.resolve():
        raise SystemExit(f"[ERROR] --session {name!r} resolves outside {SESSIONS_ROOT}")
    return target


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


# -----------------------------------------------------------------------------
# session assembly
# -----------------------------------------------------------------------------
def build_pair(
    pair: CalibrationPair, source: ReadOnlyLiveSource, render_dir: Path, clips: Path, count: int
) -> tuple[list[str], dict[str, Any]] | str:
    result = try_pair(
        pair.out_id,
        pair.in_id,
        n=CFG.TRY_N,
        context_bars=CFG.CONTEXT_BARS,
        data_dir=render_dir,
        source=source,
        keep_renders_in=render_dir / KEPT_RENDERS,
        autodj=_AUTODJ,
    )
    selection = select_taste_variants(result, count=count)
    if isinstance(selection, str):
        return selection
    lull = lull_candidate(result, selection.variants)
    chosen = [*selection.variants, *([lull] if lull is not None else [])]
    out_p, in_p = source.profile(pair.out_id), source.profile(pair.in_id)
    start_bar = shared_start_bar(result, out_p, in_p)
    features = style_vocabulary([layer2_features(c) for c in chosen])
    design = {
        "design": selection.design,
        "design_reason": selection.reason,
        "candidates_offered": len(result.candidates),
    }
    key: dict[str, Any] = {}
    cids: list[str] = []
    windows: list[tuple[float, float]] = []
    for index, (candidate, feats) in enumerate(zip(chosen, features, strict=True)):
        render_s = len(read_wav(kept_render(candidate))[0]) / RENDER_SAMPLE_RATE
        window = played_window(candidate, out_p, in_p, start_bar, render_s)
        windows.append(window)
        cid = _copy_clip(kept_render(candidate), clips, window)
        role = "lull" if candidate is lull else f"taste{index + 1}"
        key[cid] = {
            **_key_entry(pair, role, candidate, feats),
            **design,
            "played_window_s": list(window),
            "decks": _copy_decks(candidate, clips, cid, window),
        }
        cids.append(cid)
    anchor_cid = secrets.token_hex(6)
    anchor_gain_db = render_anchor(chosen[0], clips / f"{anchor_cid}.flac", windows[0])
    key[anchor_cid] = {
        **_key_entry(pair, "anchor", chosen[0], features[0]),
        **design,
        "played_window_s": list(windows[0]),
        "anchor": f"+{CFG.ANCHOR_DELAY_MS:g} ms incoming, from taste1's decks",
        "anchor_gain_db": anchor_gain_db,
        # The anchor's decks are taste1's, its incoming deck delayed like its mix (before the mix's constant gain).
        "decks": _copy_decks(chosen[0], clips, anchor_cid, windows[0], in_delay_ms=CFG.ANCHOR_DELAY_MS),
    }
    cids.append(anchor_cid)
    return cids, key


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live-data-dir", type=Path, required=True, help="the app's data dir; only read")
    parser.add_argument(
        "--render-dir", type=Path, required=True, help="scratch for each pair's kept renders, deleted after each pair"
    )
    parser.add_argument("--session", required=True, help="session name, e.g. oltf-r0")
    parser.add_argument("--pairs", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--only", help="comma-separated pair ids to build (default: all)")
    parser.add_argument(
        "--profiles",
        type=Path,
        help="read track profiles from saved snapshots here (cached_source snapshot) instead of a running engine",
    )
    return parser.parse_args(argv)


def _validated_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    """(session_dir, live, render_dir), or SystemExit; nothing on disk changes here."""
    session_dir = session_dir_for(args.session)
    if session_dir.exists():
        raise SystemExit(
            f"[ERROR] session {args.session!r} already exists at {session_dir}; its key.json is the only map from its"
            " ratings to clips. Archive or remove it yourself, or pick a new --session name."
        )
    live = args.live_data_dir.resolve()
    render_dir = args.render_dir.resolve()
    if render_dir.is_relative_to(live) or live.is_relative_to(render_dir):
        raise SystemExit(f"[ERROR] --render-dir {render_dir} contains or is inside the live data dir {live}")
    return session_dir, live, render_dir


def _selected_pairs(args: argparse.Namespace) -> tuple[list[CalibrationPair], str]:
    """The pairs to build and the fixture checksum they were verified against."""
    pairs, fixture_sha256 = load_pairs_verified(args.pairs)
    if not args.only:
        return pairs, fixture_sha256
    wanted = set(args.only.split(","))
    unknown = wanted - {p.pair_id for p in pairs}
    if unknown:
        raise SystemExit(f"[ERROR] --only names unknown pairs: {sorted(unknown)}")
    return [p for p in pairs if p.pair_id in wanted], fixture_sha256


def _require_disk(pair: CalibrationPair, *volumes: Path) -> None:
    for volume in volumes:
        if free_gb(volume) < CFG.MIN_FREE_GB:
            raise SystemExit(
                f"[ERROR] {volume}: {free_gb(volume):.1f} GB free, under {CFG.MIN_FREE_GB} GB: stopping before {pair.pair_id}"
            )


def _build_one(
    pair: CalibrationPair, source: ReadOnlyLiveSource, render_dir: Path, clips: Path
) -> tuple[list[str], dict[str, Any]] | str:
    """One pair in a builder-owned render child that is always deleted; never --render-dir itself."""
    pair_render_dir = render_dir / f"tryer-calibration-{pair.pair_id}-{secrets.token_hex(4)}"
    pair_render_dir.mkdir()
    try:
        return build_pair(pair, source, pair_render_dir, clips, CFG.TASTE_VARIANTS)
    except SKIPPABLE_PAIR_ERRORS as exc:  # the pair cannot be mixed or rendered; anything else aborts
        return f"{type(exc).__name__}: {exc}"
    except DjError as exc:
        if exc.code not in SKIPPABLE_DJ_CODES:
            raise
        return f"DjError {exc.code}: {exc}"
    finally:
        shutil.rmtree(pair_render_dir)


def _write_session(
    session_dir: Path,
    args: argparse.Namespace,
    built: list[dict[str, Any]],
    skipped: dict[str, str],
    key: dict[str, Any],
    fixture_sha256: str,
) -> None:
    manifest = {
        "session": args.session,
        "created_at": datetime.now(UTC).isoformat(),
        # The checksum the pairs were verified against at load, never a re-read after an hour of rendering.
        "pairs_fixture_sha256": fixture_sha256,
        "selection_rule": SELECTION_RULE,
        "pairs": built,
        "skipped": skipped,
    }
    manifest_text = json.dumps(manifest, indent=2) + "\n"
    (session_dir / "manifest.json").write_text(manifest_text, encoding="utf-8", newline="\n")
    key_doc = {
        "manifest_sha256": hashlib.sha256(manifest_text.encode()).hexdigest(),
        "selection_rule": SELECTION_RULE,
        "scorer_version": SCORER_VERSION,
        "candidates": key,
    }
    (session_dir / "key.json").write_text(json.dumps(key_doc, indent=2) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Validate every argument before any filesystem change.
    session_dir, live, render_dir = _validated_paths(args)
    pairs, fixture_sha256 = _selected_pairs(args)
    render_dir.mkdir(parents=True, exist_ok=True)
    # Built under a hidden sibling and renamed into place only once manifest.json and key.json are written: a failure
    # anywhere leaves no partial session under the final name, so the same name can be retried.
    staging = session_dir.with_name(f".staging-{session_dir.name}-{uuid.uuid4().hex[:8]}")
    try:
        built = _build_session(args, staging, render_dir, live, pairs, fixture_sha256)
        if built:
            staging.rename(session_dir)  # atomic on one filesystem; refuses an existing target
            print(f"[OK] wrote {(session_dir / 'manifest.json').relative_to(BENCH_ROOT)} and key.json")
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return 0 if built else 1


def _build_session(
    args: argparse.Namespace,
    staging: Path,
    render_dir: Path,
    live: Path,
    pairs: list[CalibrationPair],
    fixture_sha256: str,
) -> bool:
    """Build every pair's clips and the session's manifest and key under ``staging``; whether any pair was built."""
    clips = staging / "clips"
    clips.mkdir(parents=True)
    source = ReadOnlyLiveSource(live, args.profiles)

    built: list[dict[str, Any]] = []
    key: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    for pair in pairs:
        _require_disk(pair, render_dir, staging)
        started = time.perf_counter()
        outcome = _build_one(pair, source, render_dir, clips)
        if isinstance(outcome, str):
            skipped[pair.pair_id] = outcome
            print(f"[WARN] {pair.pair_id} skipped: {outcome}", flush=True)
            continue
        cids, pair_key = outcome
        key.update(pair_key)
        built.append({"pair_id": pair.pair_id, "candidates": [{"cid": c, "url": f"clips/{c}.flac"} for c in cids]})
        print(f"[OK] {pair.pair_id} {len(cids)} clips in {time.perf_counter() - started:.0f} s", flush=True)

    print(f"[{'OK' if built else 'ERROR'}] {len(built)} of {len(pairs)} pairs built, {len(skipped)} skipped")
    if built:
        _write_session(staging, args, built, skipped, key, fixture_sha256)
    return bool(built)


if __name__ == "__main__":
    sys.exit(main())
