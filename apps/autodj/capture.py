"""Capture a transition mixed by hand from the live mirror (MEMORY-04, ADR-0129).

The mixer lives in the page, and the one stream of it the Python side has is the UI mirror, published once a second
(``routes/state.py``). :class:`TransitionCapture` reads each published document and keeps no I/O of its own: it opens a
session when a second deck starts playing against the first, samples every control on those two decks per mirror, and
closes when the outgoing deck stops or stays silent for ``SILENT_HOLD_BARS``. A closed session is a
:class:`ClosedMix`, and :func:`in_bars` makes it the :class:`~apps.autodj.simplify.CapturedMix` that
:func:`~apps.autodj.simplify.simplify` takes.

A session records each deck's track time (``position.ms``): the mirror's ``bars_beats`` is no bar clock, since it comes
from ANLZ beat numbers, which repeat 1 to 4. :func:`in_bars` puts a closed session on the plan clock through both tracks'
own grids. A rewind or seek on either deck abandons the attempt, and because the decks are still playing the next mirror
opens a fresh one: rewinding to retry is the common case.

Never captured: a plan or AutoPlay running (save-on-run covers those), and a mix with a loop engaged, which wraps the
playhead the clock counts on and which a plan cannot express.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from apps.autodj.plan import _side_gain, bpm_gap_pct, live_child, live_mapping
from apps.autodj.profile import TrackProfile
from apps.autodj.simplify import CapturedMix, Lane, Sample

DECK_LANE_CONTROLS: tuple[str, ...] = ("fader", "trim", "filter", "eq_low", "eq_mid", "eq_high")
#: Below this channel gain the outgoing deck counts as silent.
SILENT_LEVEL = 0.01
#: This long silent and the overlap is over, though the deck may run on unheard.
SILENT_HOLD_BARS = 4.0
#: A playhead step past this between two mirrors is a seek, not a late publish.
MAX_STEP_BARS = 4.0
#: The longest overlap kept: past this two decks are simply playing.
MAX_SESSION_BARS = 256.0
#: Tempo gap (percent, half and double time folded) below which a mix was matched by hand: it replays synced.
MATCHED_TEMPO_PCT = 1.0
_RUNNING_PLAN = frozenset({"scheduled", "running"})
#: Beats in a bar of the plan clock.
_BAR_BEATS = 4.0
#: A track time this close below a downbeat is on it: float rounding must not cost a whole bar.
_DOWNBEAT_EPSILON_BARS = 1e-6


@dataclass(frozen=True)
class CaptureOutcome:
    """How the last session ended: ``closed`` or ``abandoned`` with why, then the save's verdict on a closed one
    (``saved``, ``not_saved``, ``refused``, ``failed``). ``failed`` with no tracks is a mirror capture could not read."""

    status: str
    reason: str
    out_track: str
    in_track: str


@dataclass(frozen=True)
class _DeckRead:
    deck: int
    track: str
    seconds: float
    #: Seconds per bar of this track at its grid tempo: position.ms is track time, so pitch does not change it.
    bar_s: float
    effective_bpm: float | None
    synced: bool
    looping: bool


@dataclass
class _Session:
    out: _DeckRead
    in_: _DeckRead
    tempo_mode: str
    first_out_s: float
    first_in_s: float
    last_out_s: float
    last_in_s: float
    #: Each sample's ``at_bar`` holds the outgoing track's seconds until :func:`in_bars`.
    lanes: dict[Lane, list[Sample]] = field(default_factory=dict)
    silent_since: float | None = None


@dataclass(frozen=True)
class ClosedMix:
    """One closed session on track time. :func:`in_bars` puts it on the plan clock."""

    out_deck: int
    out_track: str
    out_start_s: float
    in_deck: int
    in_track: str
    in_start_s: float
    end_s: float
    lanes: Mapping[Lane, tuple[Sample, ...]]
    tempo_mode: str


def in_bars(mix: ClosedMix, out_profile: TrackProfile, in_profile: TrackProfile) -> CapturedMix:
    """The session on the plan clock: bar 0 is the last outgoing downbeat before the overlap, and the incoming bar lined
    up with it is the one its playhead projects to."""
    first_out_bar = out_profile.bar_at(mix.out_start_s)
    out_start = math.floor(first_out_bar + _DOWNBEAT_EPSILON_BARS)

    def plan_bar(seconds: float) -> float:
        return out_profile.bar_at(seconds) - out_start

    return CapturedMix(
        out_deck=mix.out_deck,
        out_track=mix.out_track,
        out_start_bar=out_start,
        in_deck=mix.in_deck,
        in_track=mix.in_track,
        in_start_bar=max(1, round(in_profile.bar_at(mix.in_start_s) - (first_out_bar - out_start))),
        end_bar=plan_bar(mix.end_s),
        lanes={
            lane: tuple(Sample(plan_bar(sample.at_bar), sample.value) for sample in samples)
            for lane, samples in mix.lanes.items()
        },
        tempo_mode=mix.tempo_mode,
    )


# ----- reading the mirror --------------------------------------------------


def _deck_read(deck_id: str, state: Mapping[str, Any]) -> _DeckRead | None:
    """A playing deck with a track and a tempo; None for any other."""
    track = state.get("stable_id")
    if state.get("playing") is not True or not isinstance(track, str) or not track:
        return None
    grid_bpm = state.get("bpm")
    if isinstance(grid_bpm, bool) or not isinstance(grid_bpm, (int, float)) or grid_bpm <= 0:
        return None
    position_ms = _control(live_child(state, "position", "live mirror position"), "ms", int(deck_id))
    loop = state.get("loop")
    bpm = state.get("effective_bpm")
    return _DeckRead(
        deck=int(deck_id),
        track=track,
        seconds=position_ms / 1000.0,
        bar_s=_BAR_BEATS * 60.0 / float(grid_bpm),
        effective_bpm=float(bpm) if isinstance(bpm, (int, float)) else None,
        synced=live_child(state, "sync", "live mirror sync").get("enabled") is True,
        looping=isinstance(loop, Mapping) and loop.get("engaged") is True,
    )


def _playing(mirror: Mapping[str, Any]) -> dict[int, _DeckRead]:
    decks = live_child(mirror, "decks", "live mirror decks")
    reads = (_deck_read(deck_id, live_mapping(state, "live mirror deck")) for deck_id, state in decks.items())
    return {read.deck: read for read in reads if read is not None}


def _automated(mirror: Mapping[str, Any]) -> bool:
    plan = mirror.get("agent_plan")
    running = isinstance(plan, Mapping) and plan.get("status") in _RUNNING_PLAN
    return running or mirror.get("autoplay_enabled") is True


def _control(channel: Mapping[str, Any], control: str, deck: int) -> float:
    value = channel.get(control)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"deck {deck} {control} in the live mirror is {value!r}, not a number")
    return float(value)


def _lane_values(mixer: Mapping[str, Any], decks: tuple[int, int]) -> dict[Lane, float]:
    channels = live_child(mixer, "channels", "live mirror channels")
    values: dict[Lane, float] = {}
    for deck in decks:
        channel = live_child(channels, str(deck), "live mirror channel")
        values |= {(deck, control): _control(channel, control, deck) for control in DECK_LANE_CONTROLS}
    crossfader = mixer.get("crossfader")
    if isinstance(crossfader, bool) or not isinstance(crossfader, (int, float)):
        raise ValueError(f"crossfader in the live mirror is {crossfader!r}, not a number")  # noqa: TRY004 - one ValueError for every corrupt mirror value
    values[(None, "crossfader")] = float(crossfader)
    return values


def _channel_gain(mixer: Mapping[str, Any], deck: int, values: Mapping[Lane, float]) -> float:
    channel = live_child(live_child(mixer, "channels", "live mirror channels"), str(deck), "live mirror channel")
    side = _side_gain(channel.get("assign"), values[(None, "crossfader")])
    return values[(deck, "fader")] * values[(deck, "trim")] / 0.5 * side


# ----- the session ---------------------------------------------------------


def _open(out: _DeckRead, in_: _DeckRead) -> _Session:
    gap = bpm_gap_pct(out.effective_bpm, in_.effective_bpm)
    matched = in_.synced or (gap is not None and gap <= MATCHED_TEMPO_PCT)
    return _Session(
        out=out,
        in_=in_,
        tempo_mode="sync_to_out" if matched else "none",
        first_out_s=out.seconds,
        first_in_s=in_.seconds,
        last_out_s=out.seconds,
        last_in_s=in_.seconds,
    )


def _jumped(previous: float, read: _DeckRead) -> bool:
    return read.seconds < previous or read.seconds - previous > MAX_STEP_BARS * read.bar_s


class TransitionCapture:
    """One engine's capture state: feed it every accepted mirror, in order. Only the mirror route calls it."""

    def __init__(self) -> None:
        self._session: _Session | None = None
        #: (deck, track) -> the mirror tick it was first seen playing, which says who was there first.
        self._since: dict[tuple[int, str], int] = {}
        self._tick = 0
        #: A pair this overlap already closed or cannot capture: not reopened until one of them stops.
        self._spent: tuple[str, str] | None = None
        self.last_outcome: CaptureOutcome | None = None

    def note_result(self, status: str, reason: str) -> None:
        """The saver's verdict on the mix this capture last closed."""
        if self.last_outcome is None:
            raise RuntimeError("no capture has closed")
        self.last_outcome = replace(self.last_outcome, status=status, reason=reason)

    def note_fault(self, reason: str) -> None:
        """A mirror this capture could not read: the attempt under way is dropped, not guessed at."""
        session = self._session
        self._session = None
        out, in_ = ("", "") if session is None else (session.out.track, session.in_.track)
        self.last_outcome = CaptureOutcome("failed", reason, out, in_)

    def status(self) -> dict[str, Any]:
        session = self._session
        last = self.last_outcome
        return {
            "state": "idle" if session is None else "capturing",
            "out": None if session is None else session.out.track,
            "in": None if session is None else session.in_.track,
            "samples": 0 if session is None else max((len(lane) for lane in session.lanes.values()), default=0),
            "last": None
            if last is None
            else {"status": last.status, "reason": last.reason, "out": last.out_track, "in": last.in_track},
        }

    def observe(self, mirror: Mapping[str, Any]) -> ClosedMix | None:
        """One published mirror. Returns the finished mix when this one closed a session."""
        self._tick += 1
        playing = _playing(mirror)
        self._since = {
            (read.deck, read.track): self._since.get((read.deck, read.track), self._tick) for read in playing.values()
        }
        automated = _automated(mirror)
        mixer = live_child(mirror, "mixer", "live mirror mixer")
        if self._session is not None:
            finished = self._advance(self._session, playing, mixer, automated)
            if finished is not None:
                return finished
        if self._session is None and not automated:
            self._maybe_open(playing, mixer, mirror.get("master_deck"))
        return None

    def _end(self, session: _Session, status: str, reason: str, *, spent: bool) -> None:
        self.last_outcome = CaptureOutcome(status, reason, session.out.track, session.in_.track)
        self._session = None
        if spent:
            self._spent = (session.out.track, session.in_.track)

    def _maybe_open(self, playing: dict[int, _DeckRead], mixer: Mapping[str, Any], master: Any) -> None:
        tracks = {read.track for read in playing.values()}
        if self._spent is not None and not set(self._spent) <= tracks:
            self._spent = None
        if len(tracks) < 2:
            return
        # Who was playing first is the outgoing deck; the master breaks a tie (a page reload sees both at once).
        ordered = sorted(
            playing.values(), key=lambda read: (self._since[(read.deck, read.track)], read.deck != master, read.deck)
        )
        out, in_ = ordered[0], ordered[-1]
        if out.track == in_.track or (out.track, in_.track) == self._spent or out.looping or in_.looping:
            return
        session = _open(out, in_)
        self._session = session
        self._sample(session, mixer, out, in_)

    def _sample(self, session: _Session, mixer: Mapping[str, Any], out: _DeckRead, in_: _DeckRead) -> None:
        values = _lane_values(mixer, (session.out.deck, session.in_.deck))
        for lane, value in values.items():
            session.lanes.setdefault(lane, []).append(Sample(out.seconds, value))
        session.last_out_s, session.last_in_s = out.seconds, in_.seconds
        silent = _channel_gain(mixer, session.out.deck, values) < SILENT_LEVEL
        since = out.seconds if session.silent_since is None else session.silent_since
        session.silent_since = since if silent else None

    def _advance(
        self, session: _Session, playing: dict[int, _DeckRead], mixer: Mapping[str, Any], automated: bool
    ) -> ClosedMix | None:
        out, in_ = playing.get(session.out.deck), playing.get(session.in_.deck)
        if automated:
            self._end(session, "abandoned", "automation", spent=True)
        elif in_ is None or in_.track != session.in_.track:
            self._end(session, "abandoned", "incoming_stopped", spent=False)
        elif out is None or out.track != session.out.track:
            return self._close(session, "outgoing_stopped")
        else:
            return self._advance_both_playing(session, mixer, out, in_)
        return None

    def _advance_both_playing(
        self, session: _Session, mixer: Mapping[str, Any], out: _DeckRead, in_: _DeckRead
    ) -> ClosedMix | None:
        if out.looping or in_.looping:
            self._end(session, "abandoned", "loop_engaged", spent=True)
        elif _jumped(session.last_out_s, out) or _jumped(session.last_in_s, in_):
            self._end(session, "abandoned", "rewound", spent=False)
        elif out.seconds - session.first_out_s > MAX_SESSION_BARS * out.bar_s:
            self._end(session, "abandoned", "too_long", spent=True)
        else:
            self._sample(session, mixer, out, in_)
            silent_since = session.silent_since
            if silent_since is not None and out.seconds - silent_since >= SILENT_HOLD_BARS * out.bar_s:
                return self._close(session, "outgoing_silent")
        return None

    def _close(self, session: _Session, reason: str) -> ClosedMix:
        self._end(session, "closed", reason, spent=True)
        return ClosedMix(
            out_deck=session.out.deck,
            out_track=session.out.track,
            out_start_s=session.first_out_s,
            in_deck=session.in_.deck,
            in_track=session.in_.track,
            in_start_s=session.first_in_s,
            end_s=session.last_out_s if session.silent_since is None else session.silent_since,
            lanes={lane: tuple(samples) for lane, samples in session.lanes.items()},
            tempo_mode=session.tempo_mode,
        )
