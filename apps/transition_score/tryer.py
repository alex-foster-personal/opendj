"""``try_pair``: propose, render, score and rank candidates for one pair (TRYER-03).

Flow per pair (spec 10 section 3, the render loop):

1. profiles and audio paths for both tracks from a :class:`TrackSource`
   (the running engine's ``/tracks/{id}`` and ``/anlz`` routes, the same
   documents ``opendj dj`` plans from, and ``data_dir``'s state.db for the
   audio file);
2. ``apps.autodj.propose.propose_transition`` for ``n`` plans;
3. ``apps.autodj.render_plan.compile_render_plan`` for each, every candidate
   starting at the same outgoing bar (so switching candidates mid-audition
   keeps the musical position);
4. ``odj-audio render --plan --decks-out`` in a subprocess, one per worker,
   workers capped at cores minus 2, into a temporary directory of its own;
   the WAV headers are checked against the renderer's summary, the float
   WAVs rewritten as 16-bit, read back, and the directory removed;
5. :func:`apps.transition_score.scorer.score_render` on the 16-bit audio;
6. ranked best first (unscored last).

Every request renders fresh: there is no render cache (decision D7, Tue 27 Oct 2026; a cache saved 2.5 to
4.5 s of render per candidate against 5 to 7.5 s of scoring, and drew a review finding every round).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

import httpx
import numpy as np

from apps.autodj.plan import TransitionPlan, parse_plan
from apps.autodj.profile import ProfileError, TrackProfile, profile_from_payloads
from apps.autodj.propose import parse_intent, propose_transition
from apps.autodj.render_plan import (
    RenderLayout,
    RenderPlanError,
    compile_render_plan,
    effective_bars,
    layout_of,
    renders_at_start,
)
from apps.shared.engine_origin import EngineOrigin, resolve_origin, unreachable
from apps.shared.odj_audio_binary import find_binary
from apps.shared.platform_paths import PROJECT_ROOT
from apps.transition_score.contract import CORE_FACTORS, SCORER_VERSION, Candidate, Measured, TryResult, rank_key

if TYPE_CHECKING:
    from apps.transition_score.scorer import RenderContext

#: The extra that carries the try-er's audio stack (soundfile, scipy).
AUDIO_EXTRA = "analysis"


def _audio_stack() -> ModuleType:
    """soundfile, imported when a try needs it, with scipy checked beside it.

    Imported here, not at module top: the engine imports the try route at
    startup, and an install without the ``analysis`` extra must still start
    and answer a try with this error instead of failing to boot.
    """
    try:
        import scipy.signal  # noqa: F401  (the scorer's DSP; checked here so the error names the extra)
        import soundfile
    except ModuleNotFoundError as error:
        raise TryError(
            f"the try-er needs the '{AUDIO_EXTRA}' extra ({error.name} is not installed): uv sync --extra {AUDIO_EXTRA}"
        ) from error
    return soundfile


#: Each candidate renders into its own temporary directory with this prefix, removed when it is scored.
RENDER_TMP_PREFIX = "odj-try-"
#: A render longer than this is stuck, not slow (a 40 s window renders in about a second).
RENDER_TIMEOUT_S = 120.0
#: Cores left for the live engine and the page.
RESERVED_CORES = 2
OUT_DECK, IN_DECK = 1, 2
#: Deck starts and stops are seams: a deck entering mid-waveform clicks unless it ramps.
#: (A TransitionPlan has no loop control yet; a loop wrap becomes a seam here when it gets one.)
_TRANSPORT_ACTIONS = frozenset({"play", "stop"})
_LEVEL_CONTROLS = frozenset({"fader", "trim", "eq_low", "eq_mid", "eq_high", "filter"})


_log = logging.getLogger(__name__)


class TryError(RuntimeError):
    """The pair cannot be tried (no proposals, a render that failed)."""


class PairNotTryable(TryError):
    """THIS pair cannot be tried (a track without audio or usable analysis, no proposal, nothing renderable), as
    opposed to a renderer or infrastructure failure, which would hit every pair: a batch may skip only this."""


class AudioUnavailable(PairNotTryable):
    """A track's audio file is not on this machine; the message names the track only, never a server path."""

    def __init__(self, stable_id: str) -> None:
        super().__init__(f"track {stable_id} has no audio on this server: relink or re-import it")
        self.stable_id = stable_id


class TrackSource(Protocol):
    """Where a track's profile and audio come from."""

    def profile(self, stable_id: str) -> TrackProfile: ...

    def audio_path(self, stable_id: str) -> Path: ...


#: Profile resolution, the same as ``opendj dj`` reads (apps.opendj_cli.dj_service.PROFILE_POINTS).
PROFILE_POINTS = 2048
HTTP_TIMEOUT_S = 30.0


class ProfileReader(Protocol):
    def profile(self, stable_id: str) -> TrackProfile: ...


class EngineProfiles:
    """Track profiles from a running engine's HTTP API: the track and anlz payloads ``opendj dj`` reads.

    Lives here, not as a call into apps.opendj_cli, because the CLI layer sits above this domain
    package (import-linter ``webui-is-the-top-layer``); both build the profile with
    ``apps.autodj.profile.profile_from_payloads``.
    """

    def __init__(self, origin: EngineOrigin, transport: httpx.BaseTransport | None = None) -> None:
        self.origin = origin
        self._transport = transport
        self._profiles: dict[str, TrackProfile] = {}

    def _get(self, path: str) -> Any:
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT_S, transport=self._transport) as client:
                response = client.get(f"{self.origin.base_url}{path}")
        except httpx.TransportError as error:
            raise unreachable(self.origin, error) from error
        if not response.is_success:
            raise TryError(f"GET {path} -> {response.status_code}: {response.text[:300]}")
        return response.json()

    def profile(self, stable_id: str) -> TrackProfile:
        if stable_id not in self._profiles:
            quoted = quote(stable_id, safe="")
            track = self._get(f"/api/v1/tracks/{quoted}")
            anlz = self._get(f"/api/v1/tracks/{quoted}/anlz?points={PROFILE_POINTS}")
            try:
                self._profiles[stable_id] = profile_from_payloads(anlz, track)
            except ProfileError as error:
                raise PairNotTryable(f"{stable_id}: no usable analysis: {error}") from error
        return self._profiles[stable_id]


@dataclass
class EngineTrackSource:
    """Profiles from the running engine (as ``opendj dj`` reads them), audio from ``data_dir``'s state.db."""

    data_dir: Path

    def __post_init__(self) -> None:
        self._service: ProfileReader = EngineProfiles(resolve_origin())

    def profile(self, stable_id: str) -> TrackProfile:
        return self._service.profile(stable_id)

    def audio_path(self, stable_id: str) -> Path:
        from apps.stems.cli import resolve_audio_path

        return resolve_audio_path(self.data_dir, stable_id)


# ----- rendering -----------------------------------------------------------------


def odj_audio_binary() -> Path:
    """The renderer: ``ODJ_AUDIO_BIN``, else the newest cargo build in this checkout."""
    return find_binary(os.environ, PROJECT_ROOT).path


def worker_count(jobs: int) -> int:
    return max(1, min(jobs, (os.cpu_count() or 1) - RESERVED_CORES))


@dataclass(frozen=True)
class Render:
    """One render on disk for the length of one request: 16-bit mix and decks, the renderer's summary, its wall time."""

    directory: Path
    summary: dict[str, Any]
    render_ms: float

    @property
    def mix_path(self) -> Path:
        return self.directory / "mix.wav"

    def deck_path(self, deck: int) -> Path:
        return self.directory / "decks" / f"deck{deck}.wav"


#: Peak a 16-bit render is scaled to when the float render goes past full scale.
PCM16_CEILING = 0.999


def write_pcm16_set(paths: list[Path]) -> float:
    """Rewrite float WAVs as 16-bit in place, ALL by one gain, and return that gain in dB.

    A render that goes past full scale is scaled down (never clipped) so the
    clipping it would cause is still measurable: the reader divides the gain
    back out (:func:`read_render`). The same gain on the mix and every deck
    keeps their levels comparable.
    """
    sf = _audio_stack()
    audio = [sf.read(p, dtype="float32", always_2d=True) for p in paths]
    peak = max(float(np.abs(a).max(initial=0.0)) for a, _ in audio)
    if not math.isfinite(peak):
        raise TryError(f"the render holds non-finite samples (peak {peak}): refused, never scored")
    gain = min(1.0, PCM16_CEILING / peak) if peak > 0 else 1.0
    for path, (samples, sr) in zip(paths, audio, strict=True):
        temporary = path.with_suffix(".pcm16.wav")
        sf.write(temporary, samples * gain, sr, subtype="PCM_16")
        temporary.replace(path)
    return 20.0 * math.log10(gain)


def _field(summary: Mapping[str, Any], key: str, kind: type, where: str = "render summary") -> Any:
    """One renderer summary field, present and of its type, or TryError naming it: never a default."""
    if key not in summary:
        raise TryError(f"{where} has no {key!r}: the renderer's summary contract changed")
    value = summary[key]
    if kind is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    elif kind is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, kind)
    if not ok:
        raise TryError(f"{where} {key!r} is {value!r}, not a {kind.__name__}")
    return value


@dataclass(frozen=True)
class RenderTelemetry:
    """Every renderer summary field the scorer reads, checked once."""

    sample_rate: int
    duration_s: float
    master_tempo: bool
    #: (deck, tempo ratio) for every tempo point the renderer reported.
    tempo: tuple[tuple[int, float], ...]
    pcm16_gain_db: float


def parse_telemetry(summary: Mapping[str, Any], decks: tuple[int, int]) -> RenderTelemetry:
    """The summary fields the scorer depends on, or TryError naming the first that is missing or mistyped.

    Every deck the plan loads must report at least one tempo point (the renderer
    reports one at load): without them the stretch and key-shift vetoes would
    have nothing to read, and a stand-in ratio would pass them unmeasured.
    """
    sample_rate = _field(summary, "sample_rate", int)
    if sample_rate <= 0:
        raise TryError(f"render summary sample_rate is {sample_rate}")
    points: list[tuple[int, float]] = []
    for index, point in enumerate(_field(summary, "tempo", list)):
        where = f"render summary tempo[{index}]"
        if not isinstance(point, Mapping):
            raise TryError(f"{where} is not an object")
        ratio = _field(point, "tempo", float, where)
        if ratio <= 0:
            raise TryError(f"{where} tempo is {ratio}")
        points.append((_field(point, "deck", int, where), float(ratio)))
    missing = [deck for deck in decks if deck not in {d for d, _ in points}]
    if missing:
        raise TryError(
            f"render summary has no tempo telemetry for deck(s) {missing}: stretch and key shift cannot be measured"
        )
    return RenderTelemetry(
        sample_rate=sample_rate,
        # From the frame count the WAV headers were checked against, never a separately reported duration.
        duration_s=_field(summary, "frames", int) / sample_rate,
        master_tempo=_field(summary, "master_tempo", bool),
        tempo=tuple(points),
        pcm16_gain_db=float(_field(summary, "pcm16_gain_db", float)),
    )


def read_render(path: Path, summary: Mapping[str, Any]) -> np.ndarray:
    """A 16-bit render back at its float level (the PCM16 gain divided out)."""
    gain_db = float(_field(summary, "pcm16_gain_db", float))
    audio = _audio_stack().read(path, dtype="float32", always_2d=True)[0]
    return audio / np.float32(10.0 ** (gain_db / 20.0))


def _run_renderer(engine: Path, plan_path: Path, work: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(engine),
            "render",
            "--plan",
            str(plan_path),
            "--out",
            str(work / "mix.wav"),
            "--decks-out",
            str(work / "decks"),
        ],
        capture_output=True,
        text=True,
        timeout=RENDER_TIMEOUT_S,
        check=False,
    )


@contextmanager
def _refused_on_bad_renderer_output() -> Iterator[None]:
    """A missing or unreadable file the renderer was meant to write is a refusal of this candidate (TryError),
    not an error that sinks the try. Only file and audio-decoding errors are converted."""
    sf = _audio_stack()
    try:
        yield
    except (OSError, sf.SoundFileError) as error:
        raise TryError(f"renderer output missing or unreadable: {error}") from error


def _render_into(work: Path, text: str, sha: str, engine: Path) -> tuple[dict[str, Any], float]:
    """Run the renderer on ``text`` inside ``work``; the summary and the wall time, or TryError naming why not."""
    plan_path = work / "plan.json"
    plan_path.write_text(text, encoding="utf-8", newline="\n")
    started = time.perf_counter()
    try:
        completed = _run_renderer(engine, plan_path, work)
    except subprocess.TimeoutExpired as error:
        raise TryError(f"odj-audio render passed its {RENDER_TIMEOUT_S:g} s deadline: refused") from error
    except OSError as error:
        raise TryError(f"odj-audio could not start ({engine}): {error}") from error
    render_ms = (time.perf_counter() - started) * 1000.0
    if completed.returncode != 0:
        raise TryError(f"odj-audio render failed ({completed.returncode}): {completed.stderr.strip()[:500]}")
    lines = completed.stdout.strip().splitlines()
    try:
        summary = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        summary = None
    if not isinstance(summary, dict):
        raise TryError(f"the renderer exited 0 without a JSON summary line: {completed.stdout[-300:]!r}")
    if _field(summary, "plan_sha256", str) != sha:
        raise TryError(f"renderer read a different plan: {summary['plan_sha256']} != {sha}")
    return summary, render_ms


def check_wav_headers(paths: list[Path], summary: Mapping[str, Any]) -> None:
    """Each output's header must match the renderer's summary: its ``sample_rate``, ``channels`` and ``frames``
    (``channels`` is the writer's own count, ``wav.rs`` ``STEREO_CHANNELS``). A mismatch, or a summary without
    one of them, is TryError naming the file and field, so that candidate is refused."""
    sf = _audio_stack()
    infos = [(path, sf.info(path)) for path in paths]  # an unreadable file is the first thing to name
    expected = {
        "sample rate": _field(summary, "sample_rate", int),
        "channels": _field(summary, "channels", int),
        "frames": _field(summary, "frames", int),
    }
    for path, info in infos:
        found = {"sample rate": info.samplerate, "channels": info.channels, "frames": info.frames}
        for name, value in expected.items():
            if found[name] != value:
                raise TryError(f"{path.name}: WAV {name} is {found[name]}, the render summary says {value}")


def render_plan_json(document: Mapping[str, Any], directory: Path, binary: Path | None = None) -> Render:
    """Render a ``plan.rs`` document into ``directory`` (created here, and the caller's to remove).

    The renderer's float WAVs are checked against its summary, then rewritten as 16-bit by one gain.
    Anything the renderer can get wrong for this document is TryError: the caller refuses the candidate.
    """
    engine = binary or odj_audio_binary()
    text = json.dumps(document, sort_keys=True, separators=(",", ":"))
    directory.mkdir(parents=True)
    summary, render_ms = _render_into(directory, text, hashlib.sha256(text.encode()).hexdigest(), engine)
    with _refused_on_bad_renderer_output():
        decks = sorted((directory / "decks").glob("deck*.wav"))
        if not decks:
            raise TryError("the renderer wrote no deck outputs: --decks-out wrote nothing")
        outputs = [directory / "mix.wav", *decks]
        check_wav_headers(outputs, summary)
        summary["pcm16_gain_db"] = write_pcm16_set(outputs)
    return Render(directory, summary, render_ms)


# ----- scoring a render ------------------------------------------------------------


def _render_s(plan_bar: float, plan: TransitionPlan, out_profile: TrackProfile, layout: RenderLayout) -> float:
    return out_profile.time_of_bar(plan.out.start_bar + plan_bar) - layout.start_out_s


def rendered_steps(
    plan: TransitionPlan, out_profile: TrackProfile, layout: RenderLayout
) -> tuple[tuple[Any, ...], tuple[float, ...]]:
    """(level steps, transport seams) at the times the render applied them, after render time zero.

    Each event is timed at its ``effective_bars`` bar, the one ``compile_render_plan`` renders it at (a play at its
    quantized launch). An event the render applies at time zero (context start or a hoisted incoming set,
    ``renders_at_start``) is not a step in the overlap, so it is not scored at its nominal bar."""
    from apps.transition_score.scorer import Step

    bars = effective_bars(plan)
    timed = [
        (event, 0.0 if renders_at_start(event, plan, layout) else _render_s(bars[index], plan, out_profile, layout))
        for index, event in enumerate(plan.events)
    ]
    after_zero = [(e, at) for e, at in timed if at > 0]
    steps = tuple(Step(at, "out" if e.deck == plan.out.deck else "in") for e, at in after_zero if _is_level_step(e))
    seams = tuple(at for e, at in after_zero if _is_transport_seam(e))
    return steps, seams


def _is_level_step(event: Any) -> bool:
    return event.kind == "set" and event.name in _LEVEL_CONTROLS and event.deck is not None


def _is_transport_seam(event: Any) -> bool:
    return event.kind == "do" and event.name in _TRANSPORT_ACTIONS


def render_context(
    plan: TransitionPlan, out_profile: TrackProfile, layout: RenderLayout, summary: Mapping[str, Any]
) -> RenderContext:
    """What the plan meant, on the render's clock, for the scorer."""
    from apps.transition_score.scorer import RenderContext

    telemetry = parse_telemetry(summary, (plan.out.deck, plan.in_.deck))
    out_tempos = {round(ratio, 9) for deck, ratio in telemetry.tempo if deck == plan.out.deck}
    if out_tempos - {1.0}:
        raise TryError(
            f"the outgoing deck's tempo moves in this plan ({sorted(out_tempos)}); its grid on the render clock is not mapped yet"
        )

    steps, seams = rendered_steps(plan, out_profile, layout)
    return RenderContext(
        seams_s=seams,
        sample_rate=telemetry.sample_rate,
        beat_s=layout.out_bar_s / 4.0,
        overlap_s=(_render_s(0.0, plan, out_profile, layout), _render_s(float(plan.bars), plan, out_profile, layout)),
        out_downbeats_s=tuple(layout.out_downbeats_render_s(out_profile, telemetry.duration_s)),
        steps=steps,
        tempo_ratios=tuple(ratio for _, ratio in telemetry.tempo),
        pitch_follows_tempo=not telemetry.master_tempo,
        # Round 14: the scorer skips its loudness-loss veto only for an explicit wind-down plan; free text is read as
        # the proposer reads it, and a plan with no intent stays None (never assumed to be one).
        intent=None if plan.intent is None else parse_intent(plan.intent),
        # The proposer's plan shape (#7818 D13: ``lull_then_drop`` asks for the outgoing's lull); None when absent.
        shape={**plan.to_json(), **plan.extra}.get("shape"),  # #7827: shape lives in plan.extra
        bass_hit_s=_bass_hit_s(plan, out_profile, layout),
    )


def _bass_hit_s(plan: TransitionPlan, out_profile: TrackProfile, layout: RenderLayout) -> float | None:
    """The plan's incoming bass hit on the render clock (#7818 D11 ``bass_entry.hit_plan_bar``), or None without one."""
    entry = {**plan.to_json(), **plan.extra}.get("bass_entry")  # #7827: bass_entry lives in plan.extra
    if not isinstance(entry, Mapping) or entry.get("hit_plan_bar") is None:
        return None
    return _render_s(float(entry["hit_plan_bar"]), plan, out_profile, layout)


def content_bass_entry_bar(profile: TrackProfile) -> int | None:
    """The track's own bass entry (1-based bar): the first bar whose bar_bass holds at 0.8 x its p90 for 4 bars."""
    if not profile.bar_bass:
        return None
    bass = np.asarray(profile.bar_bass)
    loud = bass >= 0.8 * float(np.percentile(bass, 90))
    return next((i + 1 for i in range(len(loud) - 3) if loud[i : i + 4].all()), None)


def incoming_on_render_clock(
    plan: TransitionPlan,
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    layout: RenderLayout,
    in_audio: Path,
    frames: int,
    sample_rate: int = 48000,
) -> tuple[float | None, np.ndarray]:
    """(the incoming's content bass entry on the render clock or None, its source audio placed at its play and
    tempo-matched to the render) for round 15's early_handover and low_hole."""
    from scipy.signal import resample_poly

    bars = effective_bars(plan)
    play_bar = next(
        bars[i] for i, e in enumerate(plan.events) if e.kind == "do" and e.name == "play" and e.deck == plan.in_.deck
    )
    play_s = _render_s(play_bar, plan, out_profile, layout)
    in_start = in_profile.time_of_bar(plan.in_.start_bar + play_bar * layout.in_fold)
    entry = content_bass_entry_bar(in_profile)
    entry_s = None if entry is None else play_s + (in_profile.time_of_bar(entry) - in_start) / layout.in_ratio
    need = int((frames / sample_rate - play_s) * layout.in_ratio * sample_rate) + sample_rate
    source = outgoing_natural(in_audio, in_start, max(need, 1), sample_rate)
    stretched = resample_poly(source, 10000, round(10000 * layout.in_ratio), axis=0).astype(np.float32)
    placed = np.zeros((frames, source.shape[1]), dtype=np.float32)
    start = max(0, round(play_s * sample_rate))
    count = max(0, min(len(stretched), frames - start))
    placed[start : start + count] = stretched[:count]
    return entry_s, placed


def outgoing_natural(path: Path, start_s: float, frames: int, sample_rate: int) -> np.ndarray:
    """The outgoing track's own audio from ``start_s`` for ``frames`` at ``sample_rate`` (round 14): what it would be
    playing alone, the level the scorer holds the mix to. The try-er never moves the outgoing tempo (render_context
    refuses it), so its source and the render share one clock; past the file's end it is silence."""
    from math import gcd

    from scipy.signal import resample_poly

    soundfile = _audio_stack()
    with soundfile.SoundFile(str(path)) as source:
        rate = source.samplerate
        source.seek(min(source.frames, round(start_s * rate)))
        audio = source.read(math.ceil(frames * rate / sample_rate) + 64, dtype="float32", always_2d=True)
    if rate != sample_rate:
        common = gcd(rate, sample_rate)
        audio = resample_poly(audio, sample_rate // common, rate // common, axis=0).astype(np.float32)
    if len(audio) < frames:
        audio = np.concatenate([audio, np.zeros((frames - len(audio), audio.shape[1]), dtype=np.float32)])
    return audio[:frames]


@dataclass(frozen=True)
class _Job:
    """What every candidate of one try shares."""

    out_profile: TrackProfile
    in_profile: TrackProfile
    audio: tuple[Path, Path]
    context_bars: int
    start_out_bar: float
    binary: Path
    #: Where the caller keeps each candidate's 16-bit mix and deck WAVs (``<dir>/<cid>/``); None keeps nothing.
    keep_renders_in: Path | None = None


def _keep_render(render: Render, cid: str, job: _Job) -> Path | None:
    """Copy this candidate's 16-bit mix and deck WAVs to ``<keep_renders_in>/<cid>/`` before the temporary directory
    goes; the kept mix, or None when the caller keeps nothing. The folder must not exist: nothing is overwritten."""
    if job.keep_renders_in is None:
        return None
    target = job.keep_renders_in / cid
    target.mkdir()  # FileExistsError if it is there: never overwritten
    try:
        (target / "decks").mkdir()
        for source in [render.mix_path, *sorted((render.directory / "decks").glob("deck*.wav"))]:
            relative = source.relative_to(render.directory)
            with source.open("rb") as reader, (target / relative).open("xb") as writer:
                shutil.copyfileobj(reader, writer)
    except BaseException:
        shutil.rmtree(target)  # a partial copy never outlives its failure
        raise
    return target / "mix.wav"


def _candidate(index: int, plan: TransitionPlan, job: _Job) -> Candidate:
    out_profile, in_profile, audio = job.out_profile, job.in_profile, job.audio
    context_bars, start_out_bar = job.context_bars, job.start_out_bar
    document = compile_render_plan(
        plan, out_profile, in_profile, context_bars, out_audio=audio[0], in_audio=audio[1], start_out_bar=start_out_bar
    )
    layout = layout_of(plan, out_profile, in_profile, context_bars, start_out_bar=start_out_bar)
    with tempfile.TemporaryDirectory(prefix=RENDER_TMP_PREFIX) as scratch:
        render = render_plan_json(document, Path(scratch) / "render", job.binary)
        with _refused_on_bad_renderer_output():
            mix = read_render(render.mix_path, render.summary)
            out_deck = read_render(render.deck_path(plan.out.deck), render.summary)
            in_deck = read_render(render.deck_path(plan.in_.deck), render.summary)
        from apps.transition_score.scorer import score_render

        context = render_context(plan, out_profile, layout, render.summary)
        natural = outgoing_natural(audio[0], layout.start_out_s, len(mix), context.sample_rate)
        in_entry, in_natural = incoming_on_render_clock(plan, out_profile, in_profile, layout, audio[1], len(mix))
        context = dataclasses.replace(context, out_natural=natural, in_bass_entry_s=in_entry, in_natural=in_natural)
        started = time.perf_counter()  # the scorer alone: no WAV reads, no keep copy, no temp cleanup
        result = score_render(mix, out_deck, in_deck, context)
        score_ms = round((time.perf_counter() - started) * 1000.0, 1)
        # Kept only once validated and scored: a refusal before this point leaves no folder behind.
        kept = _keep_render(render, f"c{index + 1}", job)
    plan_json = plan.to_json()
    return Candidate(
        id=f"c{index + 1}",
        plan=plan_json,
        plan_sha256=hashlib.sha256(json.dumps(plan_json, sort_keys=True).encode()).hexdigest(),
        style=plan.style,
        bars=plan.bars,
        metrics={**result.metrics, "in_trim_db": plan.in_.trim_db, "in_trim_reason": plan.in_.trim_reason},
        factors=result.factors,
        vetoes=result.vetoes,
        measured=result.measured,
        unknown=result.unknown,
        score=result.score,
        scorer_version=SCORER_VERSION,
        render_ms=round(render.render_ms, 1),
        score_ms=score_ms,
        render_path=None if kept is None else str(kept),
    )


def _candidate_or_refusal(index: int, plan: TransitionPlan, job: _Job) -> Candidate:
    """The scored candidate, or, when its render or layout is refused, the same candidate marked refused with
    the reason. Only refusals are caught (TryError, RenderPlanError); any other error is a defect and propagates."""
    try:
        return _candidate(index, plan, job)
    except (TryError, RenderPlanError) as error:
        plan_json = plan.to_json()
        return Candidate(
            id=f"c{index + 1}",
            plan=plan_json,
            plan_sha256=hashlib.sha256(json.dumps(plan_json, sort_keys=True).encode()).hexdigest(),
            style=plan.style,
            bars=plan.bars,
            metrics={},
            factors={},
            vetoes=(),
            measured=Measured(k=0, n=len(CORE_FACTORS)),
            unknown={"render": f"refused: {error}"},
            score=None,
            scorer_version=SCORER_VERSION,
            render_ms=None,
            score_ms=0.0,
            refused=str(error),
        )


def _real_audio_paths(audio: tuple[Path, Path], stable_ids: tuple[str, str]) -> tuple[Path, Path]:
    """Each track as the real absolute file it names, so the renderer decodes the same file whatever the caller's
    working directory or symlinks. A missing file is AudioUnavailable naming the track, never the server path."""
    resolved: list[Path] = []
    for path, stable_id in zip(audio, stable_ids, strict=True):
        try:
            resolved.append(path.resolve(strict=True))
        except FileNotFoundError as error:
            _log.warning("try-er: audio for %s is gone from %s", stable_id, path)  # the path stays in the log
            raise AudioUnavailable(stable_id) from error
    return resolved[0], resolved[1]


def try_plans(
    plans: list[TransitionPlan],
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    audio: tuple[Path, Path],
    *,
    context_bars: int,
    binary: Path | None = None,
    keep_renders_in: Path | None = None,
) -> tuple[Candidate, ...]:
    """Render and score already-proposed plans, best first; all start at the same outgoing bar.

    Renders are removed once read unless the caller passes ``keep_renders_in``: then each candidate's 16-bit mix and
    deck WAVs are copied to ``<dir>/<cid>/`` (``mix.wav``, ``decks/deck<X>.wav``; ``render_path`` names the mix) and
    the caller owns them. A candidate folder already there is refused before any render; nothing is overwritten.
    """
    if not plans:
        raise PairNotTryable("no plans to try")
    _refuse_taken_keep_folders(keep_renders_in, len(plans))
    _audio_stack()  # fail before any render when the extra is missing
    audio = _real_audio_paths(audio, (out_profile.stable_id, in_profile.stable_id))
    start = shared_start_out_bar(plans, out_profile, in_profile, context_bars)
    engine = binary or odj_audio_binary()
    job = _Job(out_profile, in_profile, audio, context_bars, start, engine, keep_renders_in)
    with ThreadPoolExecutor(max_workers=worker_count(len(plans))) as pool:
        futures = [pool.submit(_candidate_or_refusal, i, p, job) for i, p in enumerate(plans)]
        candidates = [f.result() for f in futures]
    if all(c.refused is not None for c in candidates):
        raise TryError("every candidate was refused: " + "; ".join(f"{c.id}: {c.refused}" for c in candidates))
    return tuple(sorted(candidates, key=rank_key))


def _refuse_taken_keep_folders(keep_renders_in: Path | None, count: int) -> None:
    """Create ``keep_renders_in`` and refuse, before any render, a candidate folder already in it."""
    if keep_renders_in is None:
        return
    keep_renders_in.mkdir(parents=True, exist_ok=True)
    taken = [f"c{index + 1}" for index in range(count) if (keep_renders_in / f"c{index + 1}").exists()]
    if taken:
        raise FileExistsError(f"{keep_renders_in} already holds {taken}: kept renders are never overwritten")


def renderable_plans(
    plans: list[TransitionPlan], out_profile: TrackProfile, in_profile: TrackProfile, context_bars: int
) -> tuple[list[TransitionPlan], list[str]]:
    """(the plans the render can lay out, why each other one cannot); TryError when none can."""
    kept: list[TransitionPlan] = []
    refused: list[str] = []
    for plan in plans:
        try:
            layout_of(plan, out_profile, in_profile, context_bars)
            # The plan's own refusals only: the audio is checked by try_plans, so any absolute stand-in works here.
            stand_in = Path(Path.cwd().anchor)
            compile_render_plan(plan, out_profile, in_profile, context_bars, out_audio=stand_in, in_audio=stand_in)
        except RenderPlanError as error:
            refused.append(f"{plan.style}: {error}")
            continue
        kept.append(plan)
    if not kept:
        raise PairNotTryable(f"none of {len(plans)} proposals can be rendered: " + "; ".join(refused))
    return kept, refused


def shared_start_out_bar(
    plans: Sequence[TransitionPlan], out_profile: TrackProfile, in_profile: TrackProfile, context_bars: int
) -> float:
    """The one outgoing bar every render of a job starts at: the earliest any of its plans needs, so all candidates
    share a clock (switching keeps the playback position). Anything drawn over a render must use this, not a plan's own."""
    return min(layout_of(p, out_profile, in_profile, context_bars).start_out_bar for p in plans)


def try_pair(
    out_id: str,
    in_id: str,
    *,
    n: int = 6,
    context_bars: int = 4,
    data_dir: Path,
    source: TrackSource | None = None,
    binary: Path | None = None,
    keep_renders_in: Path | None = None,
) -> TryResult:
    """Propose ``n`` candidates, render and score each, return them ranked."""
    tracks = source or EngineTrackSource(Path(data_dir))
    out_profile, in_profile = tracks.profile(out_id), tracks.profile(in_id)
    proposed = propose_transition(out_profile, in_profile, out_deck=OUT_DECK, in_deck=IN_DECK, top=n)
    plans = [parse_plan(p["plan"]) for p in proposed["proposals"]]
    if not plans:
        raise PairNotTryable(
            f"no valid transition proposed for {out_id} -> {in_id} ({proposed['considered']} considered)"
        )
    proposed_count = len(plans)
    plans, refused = renderable_plans(plans, out_profile, in_profile, context_bars)
    candidates = try_plans(
        plans,
        out_profile,
        in_profile,
        (tracks.audio_path(out_id), tracks.audio_path(in_id)),
        context_bars=context_bars,
        binary=binary,
        keep_renders_in=keep_renders_in,
    )
    notes = (
        f"{proposed_count} of {n} requested candidates proposed ({proposed['considered']} considered)",
        *(f"not rendered: {reason}" for reason in refused),
    )
    return TryResult(out_id=out_id, in_id=in_id, candidates=candidates, scorer_version=SCORER_VERSION, notes=notes)
