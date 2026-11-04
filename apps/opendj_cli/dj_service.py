"""The AutoDJ tool surface over a running engine (AGENT-29).

One facade, two front ends: the MCP tools in :mod:`mcp_dj_tools` and the
``opendj dj ...`` CLI in :mod:`dj_cli` both call :class:`DjService`, so an
agent and a person typing get the same answers. The musical reasoning is the
pure :mod:`apps.autodj` package; this module only fetches what it needs over
the same HTTP routes the app already serves and posts orders on the AGENT-03
bus:

* ``GET /api/v1/tracks/{id}`` and ``/api/v1/tracks/{id}/anlz`` -> profiles
* ``GET /api/v1/state/ui-mirror`` -> the live mix state
* ``POST /api/v1/copilot/suggest-next`` -> next-track suggestions
* ``POST /api/v1/commands`` with ``{"plan": ...}`` / ``{"plan_amend": ...}``
  -> the page's plan executor (AGENT-24)

Profiles are cached per stable_id for the life of the service (an MCP server
process), because a track's analysis does not change mid-set and an agent asks
for the same two tracks many times while it plans.
"""

from __future__ import annotations

import contextlib
import math
import os
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from apps.autodj.http import (
    ANCHOR_LEAD_BARS,
    ROLES,
    MixWindow,
    PlanError,
    ProfileError,
    TrackProfile,
    TransitionPlan,
    Validation,
    build_service,
    compile_plan,
    control_command,
    first_dispatch_bar,
    live_child,
    mix_windows,
    parse_plan,
    profile_from_payloads,
    propose_transition,
    score_pair,
    score_windows,
    validate_plan,
    window_at,
)
from apps.autodj.http import (
    lead_short as _lead_short,
)
from apps.autodj.http import (
    live_bar as _live_bar,
)
from apps.autodj.http import (
    with_in_track as _with_in_track,
)
from apps.autodj.setscore import MirrorError, SetPlan, check_mirror, next_transition, plan_set, print_set_score
from apps.opendj_cli.client import (
    MIRROR_TIMEOUT_S,
    EngineClient,
    MalformedResult,
    NoPerformancePage,
    OrderRejected,
    OrderTimedOut,
)
from apps.opendj_cli.mcp_safety import MASTER_MUTE_COMMAND, SafetyRefusal, agent_audio_mode
from apps.opendj_cli.origin import EngineOrigin, unreachable
from apps.shared import file_lock

#: Waveform points requested for a profile: about ten per bar on a six-minute
#: track, enough for per-bar energy and bass, small enough to fetch quickly.
PROFILE_POINTS = 2048
AMEND_ACTIONS = ("abort", "finish_now", "extend")
#: How long ``schedule`` waits for the page to republish its mirror with the
#: incoming track on its deck after the load order succeeded. The page posts the
#: order result first and republishes after (agent-orders.ts), so the two can
#: arrive in either order; past this the load is unconfirmed, never assumed.
LOAD_CONFIRM_S = 5.0
LOAD_CONFIRM_POLL_S = 0.05
#: The partner deck AutoPlay prefers (1 <-> 2, 3 <-> 4).
_PARTNER = {1: 2, 2: 1, 3: 4, 4: 3}


#: The schedule transaction's lock: a sibling of the engine lock file, so every process on this engine shares it.
SCHEDULE_LOCK_NAME = "opendj-schedule.lock"


@contextlib.contextmanager
def _schedule_lock(path: Path) -> Iterator[None]:
    """An exclusive OS lock on ``path``, so a second schedule answers ``plan_busy`` instead of waiting or loading.

    The file is never removed: a crashed holder leaves nothing behind that could block, and no release can delete
    another holder's lock. Closing the descriptor releases the lock.
    """
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        if not file_lock.try_lock(fd):
            raise DjError("plan_busy", "another schedule_plan is already loading tracks for a plan") from None
        yield
    finally:
        os.close(fd)


class DjError(RuntimeError):
    """A refusal with a machine-readable code (MCP error documents carry it)."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields

    def document(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), **self.fields}


_MIXER_KEYS = ("fader", "trim", "eq_low", "eq_mid", "eq_high", "filter", "assign")
#: The mixer controls a plan's prepare may set on the incoming deck (its hoisted sets); ``assign`` is never set by a plan.
_PREPARE_MIXER_KEYS = ("fader", "trim", "eq_low", "eq_mid", "eq_high", "filter")
_DECK_KEYS = ("stable_id", "title", "artist", "playing", "audible", "bpm", "effective_bpm", "key", "pitch", "pitch_range", "sync")


def _deck_row(deck: Mapping[str, Any], channel: Mapping[str, Any]) -> dict[str, Any]:
    """One deck's mirror row, cut to what a planner reads."""
    row: dict[str, Any] = {key: deck.get(key) for key in _DECK_KEYS}
    row["mixer"] = {key: channel.get(key) for key in _MIXER_KEYS if key in channel}
    position = deck.get("position")
    row["position_ms"] = position.get("ms") if isinstance(position, Mapping) else None
    return row


@dataclass
class DjService:
    origin: EngineOrigin
    client_factory: Callable[[EngineOrigin], EngineClient] = EngineClient
    #: Tests hand an ``httpx.MockTransport`` here; production uses the network.
    transport: httpx.BaseTransport | None = None
    #: ``OPENDJ_AGENT_AUDIO`` as read when a call needs it; tests pass a dict.
    environ: Mapping[str, str] | None = None
    _profiles: dict[str, TrackProfile] = field(default_factory=dict)

    def _http(self) -> httpx.Client:
        return httpx.Client(timeout=MIRROR_TIMEOUT_S, trust_env=False, transport=self.transport)

    # ----- HTTP ----------------------------------------------------------
    def _get(self, path: str) -> Any:
        url = f"{self.origin.base_url}{path}"
        try:
            with self._http() as client:
                response = client.get(url)
        except httpx.TransportError as error:
            raise unreachable(self.origin, error) from error
        if response.status_code == 404:
            raise DjError("not_found", f"GET {path} -> 404: {response.text[:300]}", path=path)
        if not response.is_success:
            raise DjError(
                "upstream_error",
                f"GET {path} -> {response.status_code}: {response.text[:300]}",
                path=path,
                status_code=response.status_code,
            )
        return response.json()

    def _post(self, path: str, body: Mapping[str, Any]) -> Any:
        url = f"{self.origin.base_url}{path}"
        try:
            with self._http() as client:
                response = client.post(url, json=dict(body))
        except httpx.TransportError as error:
            raise unreachable(self.origin, error) from error
        if not response.is_success:
            raise DjError(
                "upstream_error",
                f"POST {path} -> {response.status_code}: {response.text[:300]}",
                path=path,
                status_code=response.status_code,
            )
        return response.json()

    def _audio_mode(self) -> str:
        try:
            return agent_audio_mode(self.environ)
        except SafetyRefusal as error:
            raise DjError(error.code, str(error)) from error

    def _client(self) -> EngineClient:
        return self.client_factory(self.origin)

    def _mirror(self) -> dict[str, Any]:
        try:
            return self._client().mirror()
        except NoPerformancePage as error:
            raise DjError("no_performance_page", str(error)) from error

    def _restore_deck(self, deck: int, held: Mapping[str, Any], *, load: bool = True) -> None:
        """Load a deck's previous track back (unless it was never evicted), then its cue, playhead and pitch.

        Each is restored as far as the mirror knows it. A cue press on a paused deck with no cue stores the
        playhead as the cue, so the cue is set by seeking to it and pressing cue, then the playhead is put back.
        """
        if load:
            self._order({"single": {"type": "load", "deck": deck, "stable_id": held["stable_id"]}})
        if held["cue_ms"] is not None:
            self._order({"single": {"type": "seek", "deck": deck, "position_ms": held["cue_ms"]}})
            self._order({"single": {"type": "cue", "deck": deck}})
        if held["position_ms"] is not None:
            self._order({"single": {"type": "seek", "deck": deck, "position_ms": held["position_ms"]}})
        if held["pitch"] is not None:
            self._order({"single": {"type": "tempo", "deck": deck, "ratio": held["pitch"]}})

    def _restore_prepare(self, deck: int, out_deck: int, snapshot: Mapping[str, Any]) -> None:
        """Put back what a plan's prepare sets on the incoming deck and master, as the mirror read them before the schedule."""
        for control, value in snapshot["mixer"].items():
            self._order({"single": control_command(deck, control, value)})
        if snapshot["sync"] is not None:
            self._order({"single": {"type": "beat_sync", "deck": deck, "enabled": snapshot["sync"]}})
        if snapshot["master"] is not None and snapshot["master"] != out_deck:
            self._order({"single": {"type": "master", "deck": snapshot["master"]}})
        if snapshot["muted"] is not None:
            # The mute a muted session sends with its plan is run-only: the master goes back as it was read, never persisted.
            self._order({"single": {"type": "master_mute", "muted": snapshot["muted"], "persist": False}})

    def _abort_accepted(self, plan_id: str) -> bool:
        """Abort a plan the page may hold armed, before anything is put back: its runner must not launch after a rollback.

        Returns whether the page held the plan. A page that answers that it has no such plan is not armed. Any other
        failure leaves the outcome unknown: the error is raised, and the caller puts nothing back.
        """
        try:
            self.amend(plan_id, "abort")
        except DjError as abort_error:
            # The page's runner answers an abort for a plan it does not hold with this text: only then is the plan not armed.
            if f"no plan {plan_id} on this page" in str(abort_error):
                return False
            # A plan whose prepare failed stays on the page as failed and refuses the abort. With no event fired it was
            # never armed, so the rollback may go ahead.
            # The failure is published after the page answers, so the terminal mirror is awaited rather than read once.
            if self._settled_fired(plan_id, "failed") == 0:
                return False
            raise
        return True

    def _agent_plan(self, plan_id: str) -> Mapping[str, Any] | None:
        """The page's record of ``plan_id`` from its mirror; None when the mirror does not hold that plan."""
        try:
            mirror = self._mirror()
        except DjError:
            return None
        agent_plan = mirror.get("agent_plan")
        return agent_plan if isinstance(agent_plan, Mapping) and agent_plan.get("id") == plan_id else None

    def _fired_after_abort(self, plan_id: str) -> int | None:
        """How many of the plan's events fired, read from the first mirror that shows it aborted within LOAD_CONFIRM_S.

        The page answers an abort before it republishes its mirror, so an earlier mirror can still show no event fired
        after the incoming launch has fired. None when no aborted mirror arrives: the caller then keeps the deck.
        """
        return self._settled_fired(plan_id, "aborted")

    def _settled_fired(self, plan_id: str, status: str) -> int | None:
        """How many of the plan's events fired, read from the first mirror that shows it in ``status`` within LOAD_CONFIRM_S.

        None when no mirror in that status arrives, or it does not say how many fired: the caller then keeps the deck.
        """
        deadline = time.monotonic() + LOAD_CONFIRM_S
        while True:
            agent_plan = self._agent_plan(plan_id)
            if agent_plan is not None and agent_plan.get("status") == status:
                fired = agent_plan.get("fired")
                return fired if isinstance(fired, int) and not isinstance(fired, bool) else None
            if time.monotonic() >= deadline:
                return None
            time.sleep(LOAD_CONFIRM_POLL_S)

    def _order(self, order: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._client().post_order(order)
        except NoPerformancePage as error:
            raise DjError("no_performance_page", str(error)) from error
        except (OrderRejected, OrderTimedOut, MalformedResult) as error:
            raise DjError("order_failed", str(error)) from error

    # ----- read ----------------------------------------------------------
    def profile(self, stable_id: str) -> TrackProfile:
        cached = self._profiles.get(stable_id)
        if cached is not None:
            return cached
        quoted = quote(stable_id, safe="")
        track = self._get(f"/api/v1/tracks/{quoted}")
        anlz = self._get(f"/api/v1/tracks/{quoted}/anlz?points={PROFILE_POINTS}")
        try:
            built = profile_from_payloads(anlz, track)
        except ProfileError as error:
            raise DjError("no_analysis", str(error), stable_id=stable_id) from error
        self._profiles[stable_id] = built
        return built

    def track_profile(self, stable_id: str) -> dict[str, Any]:
        return self.profile(stable_id).summary()

    def mix_state(self) -> dict[str, Any]:
        """``get_mix_state``: per deck, what a planner needs and nothing else."""
        mirror = self._mirror()
        channels = ((mirror.get("mixer") or {}).get("channels") or {})
        decks: dict[str, Any] = {}
        for deck_id, deck in sorted((mirror.get("decks") or {}).items()):
            if not isinstance(deck, Mapping):
                continue
            row = _deck_row(deck, channels.get(deck_id) or {})
            self._place_on_profile(row, deck.get("stable_id"))
            decks[deck_id] = row
        mixer = mirror.get("mixer") or {}
        return {
            "master_deck": mirror.get("master_deck"),
            "crossfader": mixer.get("crossfader") if isinstance(mixer, Mapping) else None,
            "master": mirror.get("master"),
            "autoplay_enabled": mirror.get("autoplay_enabled"),
            "autoplay_armed": mirror.get("autoplay_armed"),
            "autoplay_decision": mirror.get("autoplay_decision"),
            "agent_plan": mirror.get("agent_plan"),
            "auto_play_next": mirror.get("auto_play_next"),
            "agent_audio": self._audio_mode(),
            "decks": decks,
        }

    def _place_on_profile(self, row: dict[str, Any], stable_id: Any) -> None:
        """Add the bar, bars left and section from the track's analysis to a deck row."""
        position_ms = row["position_ms"]
        if not (isinstance(stable_id, str) and stable_id and isinstance(position_ms, (int, float))):
            return
        try:
            profile = self.profile(stable_id)
        except DjError as error:
            row["profile_error"] = error.document()
            return
        bar = profile.bar_at(float(position_ms) / 1000.0)
        section = profile.section_at(bar)
        row["bar"] = round(bar, 2)
        row["bars_total"] = profile.bars
        row["bars_left"] = max(0, round(profile.bars + 1 - bar, 2))
        row["section"] = None if section is None else {
            "label": section.label,
            "start_bar": section.start_bar,
            "bars": section.bars,
            "bars_left": max(0, round(section.end_bar - bar, 2)),
        }

    def mix_windows(self, stable_id: str, role: str, limit: int = 5) -> dict[str, Any]:
        if role not in ROLES:
            raise DjError("usage", f"role must be 'out' or 'in', got {role!r}")
        profile = self.profile(stable_id)
        return {
            "stable_id": stable_id,
            "role": role,
            "windows": [window.summary() for window in mix_windows(profile, role, limit=limit)],  # type: ignore[arg-type]
            "notes": list(profile.notes),
        }

    def score_pair(
        self,
        out_id: str,
        in_id: str,
        out_start_bar: int | None = None,
        in_start_bar: int | None = None,
        overlap_bars: int | None = None,
    ) -> dict[str, Any]:
        out_profile, in_profile = self.profile(out_id), self.profile(in_id)
        # An explicit overlap is scored as asked: a zero or negative one would be silently replaced, so it is refused.
        if overlap_bars is not None and (isinstance(overlap_bars, bool) or not isinstance(overlap_bars, int) or overlap_bars < 1):
            raise DjError("usage", f"overlap_bars must be a positive whole number of bars, got {overlap_bars!r}")
        if out_start_bar is None and in_start_bar is None:
            return score_pair(out_profile, in_profile, overlap_bars=overlap_bars).summary()
        out_window = _window_at(out_profile, "out", out_start_bar)
        in_window = _window_at(in_profile, "in", in_start_bar)
        return score_windows(
            out_profile, in_profile, out_window, in_window, overlap_bars=overlap_bars
        ).summary()

    def suggest_next(
        self, stable_id: str | None = None, session_ids: list[str] | None = None, top_n: int = 10
    ) -> dict[str, Any]:
        if stable_id is None:
            mirror = self._mirror()
            master = mirror.get("master_deck")
            deck = (mirror.get("decks") or {}).get(str(master)) if master is not None else None
            stable_id = deck.get("stable_id") if isinstance(deck, Mapping) else None
            if not stable_id:
                raise DjError("usage", "no stable_id given and no master deck holds a track")
        return self._post(
            "/api/v1/copilot/suggest-next",
            {"stable_id": stable_id, "session_ids": session_ids or [], "top_n": top_n, "explain": True},
        )

    # ----- plan ----------------------------------------------------------
    def styles(self) -> dict[str, Any]:
        return {"styles": [s.model_dump(mode="json") for s in build_service().list_styles()]}

    def propose(
        self,
        in_id: str,
        *,
        out_deck: int | None = None,
        in_deck: int | None = None,
        intent: str | None = None,
        top: int = 3,
    ) -> dict[str, Any]:
        mirror = self._mirror()
        decks = mirror.get("decks") or {}
        out_deck = out_deck if out_deck is not None else mirror.get("master_deck")
        if out_deck is None:
            raise DjError("usage", "no out_deck given and no master deck is elected")
        _check_decks(out_deck, in_deck)
        out_state = decks.get(str(out_deck)) or {}
        out_id = out_state.get("stable_id")
        if not out_id:
            raise DjError("usage", f"deck {out_deck} holds no track to mix out of")
        if in_deck is not None and (decks.get(str(in_deck)) or {}).get("quantized_launch_armed") is not None:
            # An explicit deck armed for a launch is spoken for: a plan cannot start its incoming track there.
            raise DjError("deck_busy", f"deck {in_deck} is armed to launch; pick an idle deck for the incoming track")
        in_deck = in_deck if in_deck is not None else _pick_in_deck(int(out_deck), decks, in_id)
        out_profile, in_profile = self.profile(out_id), self.profile(in_id)
        min_out_bar = _live_bar(out_profile, out_state)
        result = propose_transition(
            out_profile,
            in_profile,
            out_deck=int(out_deck),
            in_deck=in_deck,
            intent=intent,
            min_out_bar=None if min_out_bar is None else min_out_bar + ANCHOR_LEAD_BARS,
            live=_with_in_track(mirror, in_deck, in_id, in_profile.bpm),
            top=None,  # every valid candidate: the recheck below ranks what still validates, then takes the top (Sol #7595)
        )
        result["out_deck"], result["in_deck"] = int(out_deck), in_deck
        # Sol #7595: the recheck runs whether or not the first read had a playhead; only out_bar_now depends on one.
        self._recheck_proposals(result, (out_profile, in_profile), (int(out_deck), in_deck), in_id, top)
        return result

    def _recheck_proposals(
        self,
        result: dict[str, Any],
        profiles: tuple[TrackProfile, TrackProfile],
        decks: tuple[int, int],
        in_id: str,
        top: int,
    ) -> None:
        """Codex + Sol #7595: candidate search takes real time, so the mirror is read again before returning and every
        candidate is validated against it, the playhead margin and every other live precondition included. One that
        no longer validates is dropped with its reason; none left is an error, never an empty answer. A malformed
        mirror level is refused by name (``mirror_malformed``), never read as missing; a deck that changed track
        meanwhile is ``stale``."""
        (out_profile, in_profile), (out_deck, in_deck) = profiles, decks
        fresh = self._mirror()
        try:
            deck = live_child(live_child(fresh, "decks", "live mirror decks"), str(out_deck), f"live mirror deck {out_deck}")
            if deck.get("stable_id") != out_profile.stable_id:
                raise DjError("stale", f"deck {out_deck} no longer plays {out_profile.stable_id}; propose again")
            bar_now = _live_bar(out_profile, deck)  # None when the mirror carries no playhead: nothing to report
            live = _with_in_track(fresh, in_deck, in_id, in_profile.bpm)
            checked = [(row, parse_plan(row["plan"])) for row in result["proposals"]]
            checked = [(row, validate_plan(plan, out_profile, in_profile, live=live), _lead_short(plan, bar_now)) for row, plan in checked]
        except ValueError as error:
            raise DjError("mirror_malformed", f"the live mirror read after the search is malformed: {error}") from error
        kept = [{**row, "validation": validation.to_json()} for row, validation, short in checked if validation.ok and not short]
        dropped = [
            {"style": row["style"], "reason": "; ".join([*validation.errors, *([short] if short else [])])}
            for row, validation, short in checked
            if not validation.ok or short
        ]
        if bar_now is not None:
            result["out_bar_now"] = round(bar_now, 2)
        result["dropped"] = dropped
        if checked and not kept:
            raise DjError("proposals_invalidated", "every plan stopped validating while proposing: " + " | ".join(d["reason"] for d in dropped))
        result["proposals"] = kept[: max(1, top)]

    def _parsed(self, plan: Mapping[str, Any]) -> TransitionPlan:
        try:
            return parse_plan(plan)
        except PlanError as error:
            raise DjError("plan_invalid", str(error)) from error

    def validate(self, plan: Mapping[str, Any], *, live: bool = True) -> dict[str, Any]:
        parsed = self._parsed(plan)
        mirror: dict[str, Any] | None = None
        # Live by default: the outgoing deck's state and the mixer decide whether the plan can hand over, so a missing
        # page is an error, not a quiet offline check. Offline validation is an explicit ``live=False``.
        mirror = self._mirror() if live else None
        if mirror is not None:
            parsed = self._anchored(parsed, mirror)
        document = validate_plan(
            parsed,
            self.profile(parsed.out.track),
            self.profile(parsed.in_.track),
            live=None
            if mirror is None
            else _with_in_track(mirror, parsed.in_.deck, parsed.in_.track, self.profile(parsed.in_.track).bpm),
        ).to_json()
        document["plan"] = parsed.to_json()
        return document

    def _anchored(self, plan: TransitionPlan, mirror: Mapping[str, Any]) -> TransitionPlan:
        """Resolve ``next_phrase`` / ``next_bar`` to a concrete out start bar."""
        if plan.anchor == "window":
            return plan
        out_state = (mirror.get("decks") or {}).get(str(plan.out.deck)) or {}
        profile = self.profile(plan.out.track)
        bar_now = _live_bar(profile, out_state)
        if bar_now is None:
            raise DjError("usage", f"anchor {plan.anchor} needs deck {plan.out.deck}'s live position")
        # The window must start far enough ahead that the plan's EARLIEST event (a compensated blend's bar -1 pre-roll,
        # a launch lead) is still ANCHOR_LEAD_BARS ahead of the playhead, the margin validation demands (Codex #7595).
        pre_roll = max(0.0, -first_dispatch_bar(plan))
        earliest = bar_now + ANCHOR_LEAD_BARS + pre_roll
        if plan.anchor == "next_bar":
            start = math.floor(earliest) + 1
        else:
            starts = [s.start_bar for s in profile.sections if s.start_bar > earliest]
            start = starts[0] if starts else (math.floor((earliest - 1) / 8) + 1) * 8 + 1
        document = plan.to_json()
        document["out"]["window"]["start_bar"] = int(start)
        document["anchor"] = "window"
        document["anchored_from"] = plan.anchor
        return self._parsed(document)

    def schedule(self, plan: Mapping[str, Any], mirror: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Schedule ``plan`` on ``mirror``, the snapshot the caller checked it against, or on a fresh read."""
        # One schedule transaction at a time across every process on this engine: two callers that both
        # read an idle mirror would otherwise both load, and the late load could replace the first track.
        with _schedule_lock(self.origin.lock_path.with_name(SCHEDULE_LOCK_NAME)):
            return self._schedule_locked(plan, mirror)

    def _schedule_locked(self, plan: Mapping[str, Any], mirror: Mapping[str, Any] | None = None) -> dict[str, Any]:
        parsed = self._parsed(plan)
        audio = self._audio_mode()
        if mirror is None:
            mirror = self._mirror()
        # Refuse a busy page BEFORE loading: the idle incoming deck may be the one its running plan still needs.
        _refuse_active_plan(mirror)
        _refuse_autoplay_handoff(mirror)
        needs_load = _needs_load(mirror, parsed.in_.deck, parsed.in_.track)
        # Validate BEFORE touching the deck: a refused plan must not have
        # evicted whatever the idle incoming deck held.
        self._validated(parsed, _with_in_track(mirror, parsed.in_.deck, parsed.in_.track, self.profile(parsed.in_.track).bpm))
        held = _held_deck(mirror, parsed.in_.deck)
        snapshot = _incoming_snapshot(mirror, parsed.in_.deck)
        evicted = False
        changed = False  # set once a load or the plan's prepare may have touched the deck or master
        accepted = False  # set once the page has taken the plan: it may be armed, so it must be aborted before a rollback
        plan_id: str | None = None
        # Re-anchor and re-validate on the mirror the plan will actually run
        # against: the load took time, and the outgoing deck kept playing.
        try:
            if needs_load:
                evicted = True  # from here the deck may hold the new track, whatever the load or its confirmation does
                changed = True
                self._order({"single": {"type": "load", "deck": parsed.in_.deck, "stable_id": parsed.in_.track}})
                self._confirm_loaded(parsed.in_.deck, parsed.in_.track)
            # A handoff can start while the load, or the reads before it, run: the plan is compiled and submitted against
            # a mirror read here, so the refusal is checked on it, and a refusal after a load is undone by the rollback.
            mirror = self._mirror()
            _refuse_autoplay_handoff(mirror)
            parsed, validation = self._validated(parsed, mirror)
            in_profile = self.profile(parsed.in_.track)
            plan_id = f"plan-{uuid.uuid4().hex[:12]}"
            order = compile_plan(parsed, in_profile, plan_id=plan_id, live=mirror)
            if audio == "muted":
                # AGENT-25: a muted session never makes a sound, plans included.
                order["plan"]["prepare"].insert(0, dict(MASTER_MUTE_COMMAND))
                # The page checks the master is still muted before each audible play, since the operator may unmute it after schedule.
                order["plan"]["muted"] = True
            # The page runs prepare as it takes the plan, and a submit that raises may still have armed it: from here
            # the plan counts as accepted, and any refusal aborts it before the rollback.
            changed = True
            accepted = True
            steps = self._submit_plan(order, plan_id)
            # Hold the lock until the page has republished its mirror with this plan: a second schedule must not read
            # the stale mirror (no active plan yet) and load over the incoming deck this plan is about to use.
            self._confirm_scheduled(plan_id)
        except Exception:
            page_held = accepted and plan_id is not None and self._abort_accepted(plan_id)
            if changed and self._rollback_allowed(plan_id, page_held, parsed, held):
                self._put_back(parsed, held, evicted, snapshot)
            raise
        document: dict[str, Any] = {
            "plan_id": plan_id,
            "scheduled": True,
            "agent_audio": audio,
            "plan": parsed.to_json(),
            "validation": validation.to_json(),
            "notes": list(order["plan"]["notes"]),
            "page": steps[0].get("result") if steps and isinstance(steps[0], Mapping) else None,
        }
        if needs_load:
            document["loaded"] = {"deck": parsed.in_.deck, "stable_id": parsed.in_.track}
        return document

    def _rollback_allowed(
        self, plan_id: str | None, page_held: bool, parsed: TransitionPlan, held: Mapping[str, Any] | None
    ) -> bool:
        """Once the page has fired any event of a plan it holds, the incoming deck is live: a seek, load or unload
        would cut the audible mix, so the deck is left as it stands. Only a plan that never fired is rolled back, and
        only while the incoming deck still holds what the schedule left on it."""
        fired_or_held = not page_held or (plan_id is not None and self._fired_after_abort(plan_id) == 0)
        return fired_or_held and self._incoming_untouched(parsed, held)

    def _incoming_untouched(self, parsed: TransitionPlan, held: Mapping[str, Any] | None) -> bool:
        """The rollback restores the incoming deck, so it runs only while that deck still holds the track this schedule loaded
        or the one it held before (a load the page has not republished yet still shows the old one), idle and unarmed. An
        operator who has started, armed or re-loaded it since the last read keeps it as it stands, and a page that cannot be
        read is left alone rather than sent orders."""
        try:
            state = (self._mirror().get("decks") or {}).get(str(parsed.in_.deck)) or {}
        except DjError:
            return False
        held_id = held.get("stable_id") if held is not None else None
        return (
            state.get("stable_id") in (parsed.in_.track, held_id)
            and state.get("playing") is not True
            and state.get("quantized_launch_armed") is None
        )

    def _put_back(
        self, parsed: TransitionPlan, held: Mapping[str, Any] | None, evicted: bool, snapshot: Mapping[str, Any]
    ) -> None:
        """Whichever step refused the plan, leave no trace on the deck or master: the track the load evicted comes back
        with its playhead and pitch, and the mixer, Beat Sync and master are put back as read."""
        if held is not None:
            self._restore_deck(parsed.in_.deck, held, load=evicted)
        elif evicted:
            # The deck was empty before the load, so the track this schedule put on it must come off again.
            self._order({"single": {"type": "unload", "deck": parsed.in_.deck}})
        self._restore_prepare(parsed.in_.deck, parsed.out.deck, snapshot)

    def _validated(self, parsed: TransitionPlan, mirror: Mapping[str, Any]) -> tuple[TransitionPlan, Validation]:
        """The plan anchored on ``mirror`` and validated against it; refused plans raise."""
        anchored = self._anchored(parsed, mirror)
        validation = validate_plan(
            anchored, self.profile(anchored.out.track), self.profile(anchored.in_.track), live=mirror
        )
        if not validation.ok:
            raise DjError("plan_invalid", "; ".join(validation.errors), validation=validation.to_json())
        return anchored, validation

    def _confirm_loaded(self, deck: int, stable_id: str) -> dict[str, Any]:
        """The first mirror that shows ``stable_id`` on ``deck``, within LOAD_CONFIRM_S."""
        deadline = time.monotonic() + LOAD_CONFIRM_S
        while True:
            mirror = self._mirror()
            state = (mirror.get("decks") or {}).get(str(deck)) or {}
            if state.get("stable_id") == stable_id:
                return mirror
            if time.monotonic() >= deadline:
                raise DjError(
                    "load_unconfirmed",
                    f"deck {deck} was told to load {stable_id} but the page still shows "
                    f"{state.get('stable_id')!r} after {LOAD_CONFIRM_S:.0f}s; nothing was scheduled",
                    deck=deck,
                    stable_id=stable_id,
                )
            time.sleep(LOAD_CONFIRM_POLL_S)

    def _confirm_scheduled(self, plan_id: str) -> None:
        """Wait for the first mirror that shows ``plan_id`` as the page's ``agent_plan``, within LOAD_CONFIRM_S."""
        deadline = time.monotonic() + LOAD_CONFIRM_S
        while True:
            plan = self._mirror().get("agent_plan") or {}
            if plan.get("id") == plan_id:
                return
            if time.monotonic() >= deadline:
                raise DjError(
                    "plan_unconfirmed",
                    f"the page accepted plan {plan_id} but has not published it after {LOAD_CONFIRM_S:.0f}s; "
                    "check get_mix_state before scheduling again",
                    plan_id=plan_id,
                )
            time.sleep(LOAD_CONFIRM_POLL_S)

    def _submit_plan(self, order: dict[str, Any], plan_id: str) -> list[Any]:
        """Hand the compiled plan to the page; a refused step is an error, not a result."""
        steps = self._order(order).get("steps") or []
        failed = [step for step in steps if isinstance(step, Mapping) and step.get("status") == "failed"]
        if failed:
            raise DjError("plan_refused", str(failed[0].get("error")), plan_id=plan_id, steps=steps)
        return steps

    def amend(self, plan_id: str, action: str, bars: int | None = None) -> dict[str, Any]:
        if action not in AMEND_ACTIONS:
            raise DjError("usage", f"action must be one of {', '.join(AMEND_ACTIONS)}, got {action!r}")
        body: dict[str, Any] = {"id": plan_id, "action": action}
        if action == "extend":
            if bars is None or bars <= 0:
                raise DjError("usage", "extend needs bars > 0")
            body["bars"] = bars
        _refuse_muted_finish_now(plan_id, action, self._audio_mode())
        result = self._order({"plan_amend": body})
        steps = result.get("steps") or []
        failed = [step for step in steps if isinstance(step, Mapping) and step.get("status") == "failed"]
        if failed:
            raise DjError("amend_refused", str(failed[0].get("error")), plan_id=plan_id)
        return {"plan_id": plan_id, "action": action, "page": steps[0].get("result") if steps else None}

    # ----- sets (AGENT-27) -------------------------------------------------
    def plan_set(
        self,
        stable_ids: list[str],
        *,
        intent: str | None = None,
        first_deck: int | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """A whole set: the best validated transition for each neighbor pair.

        When the first track is already playing, the set starts on its deck
        and its first transition leaves after the bar the deck is at now.
        Otherwise (no page open, the track not playing, or no position) it
        plans from bar 1 on ``first_deck`` and says so in ``notes``. A mirror
        that is open but malformed is refused (``mirror_invalid``).
        """
        _check_set_args(stable_ids, first_deck)
        profiles = [self.profile(stable_id) for stable_id in stable_ids]
        live_deck, after_bar, notes = self._live_start(profiles[0])
        deck = _set_first_deck(first_deck, live_deck)
        try:
            planned = plan_set(profiles, first_deck=deck, intent=intent, name=name, after_bar=after_bar)
        except PlanError as error:
            raise DjError("plan_invalid", str(error)) from error
        document: dict[str, Any] = {"set": planned.to_json(), "score": print_set_score(planned)}
        if notes:
            document["notes"] = notes
        return document

    def _live_start(self, first: TrackProfile) -> tuple[int | None, float | None, list[str]]:
        """Where the live mix has the first track, or why it is not playing: its deck, bar, and any notes."""
        try:
            mirror = self._mirror()
        except DjError as error:
            if error.code != "no_performance_page":
                raise
            return None, None, [f"planned without the live mix: {error}"]
        try:
            decks = check_mirror(mirror)
        except MirrorError as error:
            raise DjError("mirror_invalid", str(error)) from error
        live_deck, after_bar, why_not = _playing_at(decks, first)
        return live_deck, after_bar, [f"planned without the live mix: {why_not}"] if why_not else []

    def schedule_set_next(self, planned: SetPlan) -> dict[str, Any]:
        """Schedule the set's transition that is due now, or say why none is."""
        # One snapshot for the due check and the scheduling: a track that starts on another deck between two
        # reads would otherwise be loaded twice.
        mirror = self._mirror()
        try:
            step = next_transition(planned, mirror)
        except MirrorError as error:
            raise DjError("mirror_invalid", str(error)) from error
        if step.index is None:
            return {"scheduled": False, "reason": step.reason, "transitions": len(planned.transitions)}
        document = self.schedule(planned.transitions[step.index].to_json(), mirror)
        document["set_index"] = step.index
        document["transitions"] = len(planned.transitions)
        return document

    def plan_status(self) -> dict[str, Any]:
        return {"agent_plan": self._mirror().get("agent_plan")}

    def autoplay_decision(self) -> dict[str, Any]:
        """AUTOPLAN-06: AutoPlay's next decision as the page published it."""
        return {"autoplay_decision": self._mirror().get("autoplay_decision")}

    def autoplay_decide_now(self) -> dict[str, Any]:
        """AUTOPLAN-06: the Decide now button's twin. The page answers with the decision it starts from."""
        self._order({"single": {"type": "auto_play_decide_now"}})
        return self.autoplay_decision()


def _set_first_deck(requested: int | None, live_deck: int | None) -> int:
    """The set's first deck: the live one when the first track plays, else the request, else 1."""
    if live_deck is None:
        return 1 if requested is None else requested
    if requested is not None and requested != live_deck:
        raise DjError(
            "usage",
            f"the first track is playing on D{live_deck}, so the set starts there; first_deck {requested} conflicts",
        )
    return live_deck


def _check_set_args(stable_ids: list[str], first_deck: int | None) -> None:
    """Refuse a set of fewer than two tracks, a track twice, or a first deck that is not 1..4."""
    if len(stable_ids) < 2:
        raise DjError("usage", "a set needs at least two stable_ids")
    repeated = sorted({stable_id for stable_id in stable_ids if stable_ids.count(stable_id) > 1})
    if repeated:
        raise DjError("usage", f"{repeated[0]} appears twice in the running order; a set plays each track once")
    if first_deck is not None and (type(first_deck) is not int or first_deck not in _PARTNER):
        raise DjError("usage", f"first_deck must be a deck 1..4, got {first_deck!r}")


def _playing_at(
    decks: Mapping[str, Mapping[str, Any]], profile: TrackProfile
) -> tuple[int | None, float | None, str | None]:
    """The deck playing ``profile``'s track, the bar a plan may start after, and why not live.

    ``decks`` is the checked mirror (:func:`check_mirror`). The third value is
    None when the set starts from the live mix, else what kept it from doing so.
    """
    track = profile.stable_id
    loaded = sorted(deck_id for deck_id, state in decks.items() if state["stable_id"] == track)
    playing = [deck_id for deck_id in loaded if decks[deck_id]["playing"]]
    if not playing:
        where = f"is loaded on D{loaded[0]} but not playing" if loaded else "is not on any deck"
        return None, None, f"{track} {where}, so the set starts from bar 1"
    bar = _live_bar(profile, decks[playing[0]])
    if bar is None:
        return int(playing[0]), None, f"D{playing[0]} plays {track} but publishes no position, so the set starts from bar 1"
    return int(playing[0]), bar + ANCHOR_LEAD_BARS, None


def _check_decks(out_deck: Any, in_deck: int | None) -> None:
    """A caller's deck numbers are usage errors when they are not two different decks 1..4."""
    if out_deck not in _PARTNER:
        raise DjError("usage", f"out_deck must be a deck 1..4, got {out_deck!r}")
    if in_deck is not None and (in_deck not in _PARTNER or in_deck == out_deck):
        raise DjError("usage", f"in_deck must be a deck 1..4 other than out_deck {out_deck}, got {in_deck!r}")


def _idle(state: Any) -> bool:
    """A deck free to take a plan's incoming track: not playing, and not armed for a launch (a paused launch is spoken for)."""
    return isinstance(state, Mapping) and state.get("playing") is not True and state.get("quantized_launch_armed") is None


def _pick_in_deck(out_deck: int, decks: Mapping[str, Any], in_id: str) -> int:
    """The deck already holding ``in_id``, else the idle partner, else any idle deck."""
    # A playing or armed holder is spoken for: a plan needs its incoming deck paused and free to start.
    holder = [
        int(deck_id)
        for deck_id, state in decks.items()
        if isinstance(state, Mapping) and state.get("stable_id") == in_id and int(deck_id) != out_deck and _idle(state)
    ]
    if holder:
        return holder[0]
    order = [_PARTNER[out_deck], *[d for d in (1, 2, 3, 4) if d not in (out_deck, _PARTNER[out_deck])]]
    for deck in order:
        if _idle(decks.get(str(deck))):
            return deck
    raise DjError("deck_busy", "every other deck is playing or armed; there is no deck to cue the incoming track on")


def _refuse_active_plan(mirror: Mapping[str, Any]) -> None:
    """Refuse a new plan while the page's ``agent_plan`` is scheduled or running (the page would answer ``plan_busy``)."""
    plan = mirror.get("agent_plan") or {}
    if plan.get("status") in ("scheduled", "running"):
        raise DjError("plan_busy", f"plan {plan.get('id')} is {plan['status']}; abort or finish it first")


def _refuse_muted_finish_now(plan_id: str, action: str, audio: str) -> None:
    """AGENT-25: finish_now plays the remaining steps at once and no master mute is sent with it, so a muted session would make
    a live-scheduled plan audible. The page keeps the plan's own timeline instead."""
    if action == "finish_now" and audio == "muted":
        raise DjError(
            "amend_refused",
            "a muted agent session cannot finish a plan now: the remaining steps would sound through the master; "
            "set OPENDJ_AGENT_AUDIO=live to allow it",
            plan_id=plan_id,
        )


def _refuse_autoplay_handoff(mirror: Mapping[str, Any]) -> None:
    """AUTOPLAN-05: a plan may be scheduled while AutoPlay is on, because AutoPlay yields to it (AGENT-29). Only a handoff
    already loading or starting a deck is refused, since the plan would share that deck. A mirror that does not report it
    comes from an older page, which cannot yield safely."""
    in_flight = mirror.get("autoplay_handoff_in_flight")
    if in_flight is True:
        raise DjError("autoplay_handoff", "AutoPlay is handing off a deck; schedule the plan once it lands")
    if in_flight is None and (mirror.get("autoplay_enabled") is True or mirror.get("autoplay_armed") is True):
        raise DjError("autoplay_handoff", "this page does not report AutoPlay handoffs; reload it before scheduling with AutoPlay on")


def _sync_enabled(sync: Any) -> bool | None:
    """Beat Sync's enabled flag from the mirror's ``sync`` object ``{mode, enabled}``; None when it is not reported."""
    enabled = sync.get("enabled") if isinstance(sync, Mapping) else None
    return enabled if isinstance(enabled, bool) else None


def _incoming_snapshot(mirror: Mapping[str, Any], deck: int) -> dict[str, Any]:
    """The incoming deck's mixer values, Beat Sync flag and the master, as the mirror reports them before a schedule."""
    state = (mirror.get("decks") or {}).get(str(deck)) or {}
    channel = ((mirror.get("mixer") or {}).get("channels") or {}).get(str(deck)) or {}
    master = mirror.get("master_deck")
    master_state = mirror.get("master")
    muted = master_state.get("muted") if isinstance(master_state, Mapping) else None
    return {
        "mixer": {key: channel[key] for key in _PREPARE_MIXER_KEYS if isinstance(channel.get(key), (int, float))},
        "sync": _sync_enabled(state.get("sync")),
        "master": master if isinstance(master, int) else None,
        "muted": muted if isinstance(muted, bool) else None,
    }


def _held_deck(mirror: Mapping[str, Any], deck: int) -> dict[str, Any] | None:
    """What a deck holds on the mirror (track, cue, playhead, pitch), or None when it is empty."""
    state = (mirror.get("decks") or {}).get(str(deck)) or {}
    stable_id = state.get("stable_id")
    if not (isinstance(stable_id, str) and stable_id):
        return None
    position = state.get("position")
    position_ms = position.get("ms") if isinstance(position, Mapping) else None
    cue_ms = state.get("cue_ms")
    pitch = state.get("pitch")
    return {
        "stable_id": stable_id,
        "cue_ms": float(cue_ms) if isinstance(cue_ms, (int, float)) else None,
        "position_ms": float(position_ms) if isinstance(position_ms, (int, float)) else None,
        "pitch": float(pitch) if isinstance(pitch, (int, float)) and pitch > 0 else None,
    }


def _needs_load(mirror: Mapping[str, Any], in_deck: int, in_id: str) -> bool:
    """Whether ``schedule`` must load ``in_id``; a playing or armed deck is busy, even one already holding the track."""
    state = (mirror.get("decks") or {}).get(str(in_deck)) or {}
    if state.get("playing") is True:
        raise DjError(
            "deck_busy",
            f"deck {in_deck} is playing; pick an idle deck for the incoming track",
        )
    if state.get("quantized_launch_armed") is not None:
        # A load would replace the track a pending launch is about to play, and a seek or tempo would change that launch.
        raise DjError(
            "deck_busy",
            f"deck {in_deck} is armed to launch; pick an idle deck for the incoming track",
        )
    return state.get("stable_id") != in_id


def _window_at(profile: TrackProfile, role: str, start_bar: int | None) -> MixWindow:
    if start_bar is None:
        return mix_windows(profile, role, limit=1)[0]  # type: ignore[arg-type]
    try:
        return window_at(profile, role, start_bar)  # type: ignore[arg-type]
    except ValueError as error:
        raise DjError("usage", str(error)) from error
