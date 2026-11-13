"""DjService (AGENT-29): the AutoDJ tools over a fake engine and a fake page.

HTTP reads go through an ``httpx.MockTransport`` that serves invented
``/tracks`` and ``/anlz`` payloads; the AGENT-02 mirror and AGENT-03 orders go
through a fake :class:`EngineClient` that records each order and answers the
way ``executeAgentOrder`` in agent-orders.ts does.
"""

from __future__ import annotations

import copy
import json
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from apps.autodj import http as autodj_http
from apps.autodj import propose as autodj_propose
from apps.autodj import setscore as autodj_setscore
from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_PAGE, EXIT_UNCONFIRMED, dj_cli
from apps.opendj_cli import dj_service as _dj_service_mod
from apps.opendj_cli.client import NoPerformancePage
from apps.opendj_cli.dj_service import DjError, DjService, _held_deck
from apps.opendj_cli.origin import EngineOrigin
from tests.autodj.fixtures import anlz_payload, profile, track_row

_ENGINE_DIR = tempfile.TemporaryDirectory()  # the schedule lock is created beside the engine lock file
ORIGIN = EngineOrigin(
    host="127.0.0.1", port=1, lock_path=Path(_ENGINE_DIR.name) / ".engine.lock", pid=None, role=None, boot_id=None
)
TRACKS = {"trk-out": ("8A", 128.0), "trk-in": ("9A", 128.0), "trk-next": ("8B", 127.0)}
OUT = profile("trk-out")


def _transport(requests: list[str]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        parts = request.url.path.split("/")
        if request.url.path == "/api/v1/copilot/suggest-next":
            body = json.loads(request.content)
            return httpx.Response(200, json={"seed": body["stable_id"], "suggestions": [{"stable_id": "trk-in"}]})
        if len(parts) >= 5 and parts[3] == "tracks" and parts[4] in TRACKS:
            key, bpm = TRACKS[parts[4]]
            if parts[-1] == "anlz":
                return httpx.Response(200, json=anlz_payload(parts[4], bpm=bpm))
            return httpx.Response(200, json=track_row(parts[4], bpm=bpm, key=key))
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handle)


class FakePage:
    """The AGENT-02/03 page: a mirror, and orders answered like agent-orders.ts."""

    def __init__(self, *, out_bar: float = 100.0, out_playing: bool = True, page_open: bool = True) -> None:
        self.page_open = page_open
        self.orders: list[dict[str, Any]] = []
        self.refuse_plan: str | None = None
        #: How many mirror reads after a load still show the OLD deck: the page
        #: posts the order result first and republishes after (agent-orders.ts).
        #: None means the republish never lands.
        self.republish_lag: int | None = 0
        self._pending_load: tuple[str, str] | None = None
        self._lag_left = 0
        self.mirror_reads = 0
        self.plan_unpublished = False  # control: the page accepts a plan but never republishes it
        #: an abort is answered before the page republishes its mirror (as with a load): the mirror keeps the pre-abort
        #: snapshot for this many reads, and fired_on_abort events and the deck's playing state land with the aborted mirror.
        self.abort_republish_lag = 0
        self.fired_on_abort: int | None = None  # None keeps the plan's count as the page held it
        self._abort_publication: Callable[[], None] | None = None
        self._abort_lag_left = 0
        self.mirror_doc: dict[str, Any] = {
            "master_deck": 1,
            "mixer": {
                "crossfader": 0.5,
                "channels": {"1": {"fader": 1.0, "trim": 0.5, "assign": "THRU"}, "2": {"fader": 1.0}},
            },
            "decks": {
                "1": {
                    "stable_id": "trk-out",
                    "title": "Invented trk-out",
                    "playing": out_playing,
                    "position": {"ms": OUT.time_of_bar(out_bar) * 1000.0},
                },
                "2": {"stable_id": None, "playing": False},
                "3": {"stable_id": None, "playing": False},
                "4": {"stable_id": None, "playing": False},
            },
            "agent_plan": None,
        }

    def __call__(self, origin: EngineOrigin) -> FakePage:  # the client_factory
        return self

    def mirror(self) -> dict[str, Any]:
        if not self.page_open:
            raise NoPerformancePage("no performance page is open")
        self.mirror_reads += 1
        if self._pending_load is not None and self.republish_lag is not None:
            if self._lag_left <= 0:
                deck, stable_id = self._pending_load
                self.mirror_doc["decks"][deck]["stable_id"] = stable_id
                self._pending_load = None
            self._lag_left -= 1
        if self._abort_publication is not None:
            if self._abort_lag_left <= 0:
                self._abort_publication()
                self._abort_publication = None
            self._abort_lag_left -= 1
        return self.mirror_doc

    def post_order(self, order: dict[str, Any]) -> dict[str, Any]:
        if not self.page_open:
            raise NoPerformancePage("no performance page is open")
        self.orders.append(order)
        if "single" in order and order["single"]["type"] == "load":
            command = order["single"]
            self._pending_load = (str(command["deck"]), command["stable_id"])
            self._lag_left = self.republish_lag or 0
            return {"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}}
        if "plan" in order:
            if self.refuse_plan:
                return {"steps": [{"status": "failed", "error": self.refuse_plan}], "mirror_delta": {}}
            plan_id = order["plan"]["id"]
            if not self.plan_unpublished:  # the page republishes its mirror with the plan, as the real page does
                self.mirror_doc["agent_plan"] = {"id": plan_id, "status": "scheduled", "fired": 0}
            return {
                "steps": [{"status": "succeeded", "result": {"plan_id": plan_id, "status": "scheduled", "bar": 100.0}}],
                "mirror_delta": {},
            }
        if "plan_amend" in order:
            plan_id = order["plan_amend"]["id"]
            if (self.mirror_doc.get("agent_plan") or {}).get(
                "id"
            ) != plan_id:  # an abort for a plan the page does not hold
                return {"steps": [{"status": "failed", "error": f"no plan {plan_id} on this page"}], "mirror_delta": {}}
            fired = self.fired_on_abort
            if fired is None:  # the events the page had fired when it took the abort
                fired = (self.mirror_doc.get("agent_plan") or {}).get("fired", 0)

            def publish_abort() -> None:
                self.mirror_doc["agent_plan"] = {"id": plan_id, "status": "aborted", "fired": fired}
                if fired:  # the incoming launch the aborted runner fired is audible now
                    self.mirror_doc["decks"]["2"]["playing"] = True

            self._abort_publication = publish_abort
            self._abort_lag_left = self.abort_republish_lag
            return {
                "steps": [{"status": "succeeded", "result": {"plan_id": plan_id, "status": "aborted"}}],
                "mirror_delta": {},
            }
        if "single" in order:  # any bus command, as agent-orders.ts runs it through the same dispatch
            return {"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}}
        raise AssertionError(f"unexpected order {order}")


@pytest.fixture
def requests() -> list[str]:
    return []


_PROPOSE_TRANSITION_CACHE: dict[str, dict[str, Any]] = {}


def _propose_transition_cache_key(args: tuple[object, ...], kwargs: Mapping[str, Any]) -> str:
    # Omit `live`: the search already gets the playhead as min_out_bar, and these tests share one mixer.
    # Round the playhead so a bar→ms→bar round-trip still hits.
    out_profile = args[0] if args else kwargs.get("out_profile")
    in_profile = args[1] if len(args) > 1 else kwargs.get("in_profile")
    min_out_bar = kwargs.get("min_out_bar")
    if isinstance(min_out_bar, (int, float)) and not isinstance(min_out_bar, bool):
        min_out_bar = round(float(min_out_bar), 3)
    around_bar = kwargs.get("around_bar")
    if isinstance(around_bar, (int, float)) and not isinstance(around_bar, bool):
        around_bar = round(float(around_bar), 3)
    payload = {
        "out": getattr(out_profile, "stable_id", None),
        "in": getattr(in_profile, "stable_id", None),
        # A test may drop the incoming bass analysis, which moves blend entries: that is a different search.
        "in_bass_analyzed": getattr(in_profile, "bar_bass", None) is not None,
        "out_deck": kwargs.get("out_deck"),
        "in_deck": kwargs.get("in_deck"),
        "intent": kwargs.get("intent"),
        "min_out_bar": min_out_bar,
        "top": kwargs.get("top"),
        "around_bar": around_bar,
    }
    return json.dumps(payload, sort_keys=True, default=str)


@pytest.fixture(autouse=True)
def _reuse_deterministic_proposals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reuse propose_transition results: it is deterministic and ~5s, and this module calls it
    dozens of times. Recheck still runs on a deep copy. Without this, the file exceeds the
    pre-PR gate's 420s wall clock."""
    real = autodj_propose.propose_transition

    def cached(*args: object, **kwargs: object) -> dict[str, Any]:
        key = _propose_transition_cache_key(args, kwargs)
        hit = _PROPOSE_TRANSITION_CACHE.get(key)
        if hit is None:
            hit = real(*args, **kwargs)
            _PROPOSE_TRANSITION_CACHE[key] = hit
        return copy.deepcopy(hit)  # callers mutate the document; the cache keeps a clean copy

    for module in (autodj_propose, autodj_setscore, autodj_http, _dj_service_mod):
        monkeypatch.setattr(module, "propose_transition", cached)


def _service(page: FakePage, requests: list[str], audio: str | None = None) -> DjService:
    environ = {} if audio is None else {"OPENDJ_AGENT_AUDIO": audio}
    return DjService(origin=ORIGIN, client_factory=page, transport=_transport(requests), environ=environ)  # type: ignore[arg-type]


def test_mix_state_reports_bar_and_section_from_the_live_position(requests: list[str]) -> None:
    state = _service(FakePage(out_bar=114.5), requests).mix_state()
    deck = state["decks"]["1"]
    assert deck["bar"] == pytest.approx(114.5, abs=0.01)
    assert deck["section"]["label"] == "outro"
    assert deck["bars_left"] == pytest.approx(14.5, abs=0.01)
    assert deck["mixer"] == {"fader": 1.0, "trim": 0.5, "assign": "THRU"}
    assert "bar" not in state["decks"]["2"]  # no track, no bar
    assert state["master_deck"] == 1 and state["crossfader"] == 0.5


def test_profiles_are_cached_per_service(requests: list[str]) -> None:
    service = _service(FakePage(), requests)
    service.track_profile("trk-in")
    service.mix_windows("trk-in", "in")
    assert requests.count("/api/v1/tracks/trk-in/anlz") == 1


def test_unknown_track_is_not_found_and_bad_role_is_usage(requests: list[str]) -> None:
    service = _service(FakePage(), requests)
    with pytest.raises(DjError) as missing:
        service.track_profile("trk-missing")
    assert missing.value.code == "not_found"
    with pytest.raises(DjError) as usage:
        service.mix_windows("trk-in", "sideways")
    assert usage.value.code == "usage"


def test_suggest_next_defaults_to_the_master_track(requests: list[str]) -> None:
    result = _service(FakePage(), requests).suggest_next()
    assert result["seed"] == "trk-out"


def test_propose_picks_the_partner_deck_and_starts_after_the_live_bar(requests: list[str]) -> None:
    result = _service(FakePage(out_bar=100.0), requests).propose("trk-in", intent="smooth")
    assert (result["out_deck"], result["in_deck"]) == (1, 2)
    assert result["proposals"]
    for row in result["proposals"]:
        assert row["plan"]["out"]["window"]["start_bar"] > 100
        assert row["validation"]["ok"] is True


def test_schedule_loads_the_idle_deck_then_posts_one_plan_order(requests: list[str]) -> None:
    page = FakePage(out_bar=100.0)
    service = _service(page, requests)
    plan = service.propose("trk-in")["proposals"][0]["plan"]
    result = service.schedule(plan)
    assert result["scheduled"] is True and result["plan_id"].startswith("plan-")
    assert result["loaded"] == {"deck": 2, "stable_id": "trk-in"}
    assert [next(iter(order)) for order in page.orders] == ["single", "plan"]
    order = page.orders[1]["plan"]
    assert order["id"] == result["plan_id"]
    # Sol P1 #7595: the compiler's notes reach the agent with the result, as the order carries them to the page.
    assert isinstance(order["notes"], list) and result["notes"] == order["notes"]
    assert order["clock"] == {"deck": 1, "stable_id": "trk-out"}
    assert any(c["type"] == "seek" and c["deck"] == 2 for c in order["prepare"])


def test_schedule_holds_the_lock_until_the_page_publishes_the_plan(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the page accepts a plan but never publishes it [then] schedule says so, not success, [else stop]."""
    from apps.opendj_cli import dj_service

    monkeypatch.setattr(dj_service, "LOAD_CONFIRM_S", 0.2)
    page = FakePage(out_bar=100.0)
    service = _service(page, requests)
    plan = service.propose("trk-in")["proposals"][0]["plan"]
    page.plan_unpublished = True
    with pytest.raises(DjError) as unconfirmed:
        service.schedule(plan)
    assert unconfirmed.value.code == "plan_unconfirmed"
    # Control: the same schedule once the page publishes the plan succeeds.
    page.plan_unpublished = False
    assert service.schedule(plan)["scheduled"] is True


def test_schedule_waits_for_the_republished_mirror_after_a_load(requests: list[str]) -> None:
    page = FakePage(out_bar=100.0)
    service = _service(page, requests)
    plan = service.propose("trk-in")["proposals"][0]["plan"]
    page.republish_lag = 3
    reads_before = page.mirror_reads
    result = service.schedule(plan)
    assert result["loaded"] == {"deck": 2, "stable_id": "trk-in"}
    assert page.mirror_reads - reads_before >= 1 + 3 + 1, "schedule read past the stale mirrors"
    assert page.mirror_doc["decks"]["2"]["stable_id"] == "trk-in"
    assert [next(iter(order)) for order in page.orders] == ["single", "plan"]


def test_schedule_refuses_a_load_the_page_never_confirms(requests: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.opendj_cli import dj_service

    monkeypatch.setattr(dj_service, "LOAD_CONFIRM_S", 0.2)
    page = FakePage(out_bar=100.0)
    service = _service(page, requests)
    plan = service.propose("trk-in")["proposals"][0]["plan"]
    page.republish_lag = None
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "load_unconfirmed"
    # Nothing is armed. The deck was empty and the load may have landed, so the track comes off again, then the
    # incoming channel's fader goes back as the mirror read it.
    assert [order["single"] for order in page.orders] == [
        {"type": "load", "deck": 2, "stable_id": "trk-in"},
        {"type": "unload", "deck": 2},
        {"type": "fader", "deck": 2, "value": 1.0},
    ]


def test_an_invalid_plan_is_refused_before_the_incoming_deck_is_loaded(requests: list[str]) -> None:
    page = FakePage(out_bar=100.0)
    page.mirror_doc["decks"]["2"]["stable_id"] = "trk-next"  # cued, idle, about to be replaced
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]
    page.mirror_doc["decks"]["1"]["playing"] = False  # the outgoing deck stopped: the plan is now invalid
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "plan_invalid"
    assert page.orders == [], "nothing was sent, so the cued track stayed"
    assert page.mirror_doc["decks"]["2"]["stable_id"] == "trk-next"


def test_propose_refuses_decks_outside_one_to_four_as_usage(requests: list[str]) -> None:
    service = _service(FakePage(out_bar=100.0), requests)
    for kwargs in ({"out_deck": 7}, {"out_deck": 0}, {"out_deck": 1, "in_deck": 5}, {"out_deck": 1, "in_deck": 1}):
        with pytest.raises(DjError) as refused:
            service.propose("trk-in", **kwargs)  # type: ignore[arg-type]
        assert refused.value.code == "usage", kwargs


def test_schedule_refuses_to_start_from_silence(requests: list[str]) -> None:
    page = FakePage(out_bar=100.0)
    service = _service(page, requests)
    plan = service.propose("trk-in")["proposals"][0]["plan"]  # a valid plan while the outgoing deck plays
    page.mirror_doc["decks"]["1"]["playing"] = False  # then the outgoing deck stops before the schedule
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "plan_invalid"
    assert "a plan continues a playing set" in str(refused.value)
    assert not any("plan" in order for order in page.orders)  # nothing armed


_MUTE = {"type": "master_mute", "muted": True, "persist": False}


@pytest.mark.requirement("AGENT-25")
def test_a_muted_session_mutes_the_master_first_in_every_plan(requests: list[str]) -> None:
    """[if] the agent session is muted [then] prepare starts with the master mute, [else stop]."""
    page = FakePage(out_bar=100.0)
    service = _service(page, requests)
    plan = service.propose("trk-in")["proposals"][0]["plan"]
    result = service.schedule(plan)
    assert result["agent_audio"] == "muted"
    assert page.orders[1]["plan"]["prepare"][0] == _MUTE
    assert page.orders[1]["plan"]["muted"] is True
    assert service.mix_state()["agent_audio"] == "muted"


@pytest.mark.requirement("AGENT-25")
def test_a_live_session_never_mutes_a_plan(requests: list[str]) -> None:
    """[if] OPENDJ_AGENT_AUDIO=live [then] no master mute goes anywhere in the plan, [else stop]."""
    page = FakePage(out_bar=100.0)
    service = _service(page, requests, audio="live")
    plan = service.propose("trk-in")["proposals"][0]["plan"]
    result = service.schedule(plan)
    assert result["agent_audio"] == "live"
    order = page.orders[1]["plan"]
    commands = [*order["prepare"], *(e.get("command") or e["ramp"]["command"] for e in order["events"])]
    assert all(c["type"] != "master_mute" for c in commands)
    assert any(c["type"] == "seek" for c in order["prepare"])  # control: prepare is not empty


@pytest.mark.requirement("AGENT-25")
def test_an_unknown_agent_audio_value_is_refused_before_anything_is_sent(requests: list[str]) -> None:
    """[if] OPENDJ_AGENT_AUDIO is not muted or live [then] schedule refuses it, [else stop]."""
    page = FakePage(out_bar=100.0)
    service = _service(page, requests, audio="loud")
    plan = _service(FakePage(out_bar=100.0), requests).propose("trk-in")["proposals"][0]["plan"]
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "agent_audio_invalid"
    assert page.orders == []


def test_propose_refuses_an_explicit_incoming_deck_armed_for_a_launch(requests: list[str]) -> None:
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": None, "playing": False, "quantized_launch_armed": {"bar": 9}}
    with pytest.raises(DjError) as armed:
        _service(page, requests).propose("trk-in", in_deck=2)
    assert armed.value.code == "deck_busy"
    # Control: the same deck, paused and not armed, is accepted.
    page.mirror_doc["decks"]["2"] = {"stable_id": None, "playing": False}
    assert _service(page, requests).propose("trk-in", in_deck=2)["proposals"]


def test_schedule_puts_back_the_track_a_refused_load_evicted(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan that clears validation before its load but not after it puts the idle deck's old track back."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-old", "playing": False}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]
    validated = service._validated
    calls: list[int] = []

    def refuse_after_the_load(parsed: Any, mirror: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:  # the pre-load check passes; the check on the loaded mirror refuses
            raise DjError("plan_invalid", "refused after the load")
        return validated(parsed, mirror)

    monkeypatch.setattr(service, "_validated", refuse_after_the_load)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "plan_invalid"
    loads = [order["single"]["stable_id"] for order in page.orders if order.get("single", {}).get("type") == "load"]
    assert loads == ["trk-in", "trk-old"]  # the new track loads, then the old one is put back
    assert not any("plan" in order for order in page.orders)  # nothing armed


def test_schedule_puts_back_the_track_when_submitting_the_plan_fails(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submit that fails after the load still puts the idle deck's old track back: the rollback covers the whole schedule."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {
        "stable_id": "trk-old",
        "playing": False,
        "position": {"ms": 61000.0},
        "pitch": 1.04,
    }
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def refuse_the_submit(order: Any, plan_id: str) -> Any:
        raise DjError("plan_failed", "the page refused the prepare step")

    monkeypatch.setattr(service, "_submit_plan", refuse_the_submit)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "plan_failed"
    assert [order["plan_amend"]["action"] for order in page.orders if "plan_amend" in order] == ["abort"]
    assert [order["single"] for order in page.orders if "single" in order] == [
        {"type": "load", "deck": 2, "stable_id": "trk-in"},
        {"type": "load", "deck": 2, "stable_id": "trk-old"},  # the old track comes back
        {
            "type": "seek",
            "deck": 2,
            "position_ms": 61000.0,
        },  # with its playhead and pitch; the cue is not in the mirror
        {"type": "tempo", "deck": 2, "ratio": 1.04},
        {"type": "fader", "deck": 2, "value": 1.0},  # the channel's fader, as the mirror read it before the schedule
    ]


def test_schedule_puts_back_the_track_when_the_load_is_never_confirmed(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load the page never confirms may already have evicted the idle deck's track: it comes back, and nothing is scheduled."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-old", "playing": False, "pitch": 1.0}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def never_confirmed(deck: int, stable_id: str) -> Any:
        raise DjError("load_unconfirmed", "the page did not publish the load")

    monkeypatch.setattr(service, "_confirm_loaded", never_confirmed)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "load_unconfirmed"
    assert [order["single"] for order in page.orders] == [
        {"type": "load", "deck": 2, "stable_id": "trk-in"},
        {"type": "load", "deck": 2, "stable_id": "trk-old"},
        {"type": "tempo", "deck": 2, "ratio": 1.0},
        {"type": "fader", "deck": 2, "value": 1.0},
    ]  # the old track comes back; no plan order is sent


def test_a_refused_submit_puts_back_what_prepare_changed_when_the_track_was_already_loaded(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the incoming track already loaded nothing is evicted, but a prepare that ran before a refused step still
    moved the playhead, Beat Sync and the channel's mixer: each goes back to what the mirror read before the schedule."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {
        "stable_id": "trk-in",
        "playing": False,
        "position": {"ms": 5000.0},
        "pitch": 1.02,
        "sync": {"mode": "sync_to_out", "enabled": True},
    }
    page.mirror_doc["mixer"]["channels"]["2"] = {"fader": 0.4, "trim": 0.9}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def prepare_then_refuse(order: Any, plan_id: str) -> Any:
        # The page runs prepare first: its seek and Beat Sync are applied before the step that refuses the plan.
        service._order({"single": {"type": "seek", "deck": 2, "position_ms": 61000.0}})
        service._order({"single": {"type": "beat_sync", "deck": 2, "enabled": False}})
        raise DjError("plan_refused", "the page refused a later step")

    monkeypatch.setattr(service, "_submit_plan", prepare_then_refuse)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "plan_refused"
    assert [order["plan_amend"]["action"] for order in page.orders if "plan_amend" in order] == ["abort"]
    orders = [order["single"] for order in page.orders if "single" in order]
    assert not any(order["type"] == "load" for order in orders), "nothing was evicted, so nothing is reloaded"
    assert orders[2:] == [  # after the two prepare-side orders above
        {"type": "seek", "deck": 2, "position_ms": 5000.0},
        {"type": "tempo", "deck": 2, "ratio": 1.02},
        {"type": "fader", "deck": 2, "value": 0.4},
        {"type": "trim", "deck": 2, "value": 0.9},
        {"type": "beat_sync", "deck": 2, "enabled": True},
    ]


def test_a_plan_the_page_accepted_is_aborted_before_its_deck_is_put_back(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan the page took but never published may be armed: it is aborted first, so nothing launches after the rollback."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-in", "playing": False, "position": {"ms": 5000.0}}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def accepted_but_unpublished(plan_id: str) -> None:
        raise DjError("plan_unconfirmed", "the page accepted the plan but has not published it", plan_id=plan_id)

    monkeypatch.setattr(service, "_confirm_scheduled", accepted_but_unpublished)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "plan_unconfirmed"
    plan_id = refused.value.fields["plan_id"]
    orders = page.orders
    abort_at = next(i for i, order in enumerate(orders) if "plan_amend" in order)
    assert orders[abort_at] == {"plan_amend": {"id": plan_id, "action": "abort"}}
    assert orders[abort_at + 1 :], "the deck is put back after the abort"
    assert all("single" in order for order in orders[abort_at + 1 :])


def test_a_plan_that_already_fired_is_not_rolled_back_when_its_confirmation_fails(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan whose first event fired before its confirmation failed is live: a seek or reload would cut the mix."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-in", "playing": False, "position": {"ms": 5000.0}}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def fired_then_unconfirmed(plan_id: str) -> None:
        page.mirror_doc["agent_plan"] = {"id": plan_id, "status": "running", "fired": 1, "total": 3}
        raise DjError("plan_unconfirmed", "the page did not publish the plan", plan_id=plan_id)

    monkeypatch.setattr(service, "_confirm_scheduled", fired_then_unconfirmed)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "plan_unconfirmed"
    plan_id = refused.value.fields["plan_id"]
    orders = page.orders
    abort_at = next(i for i, order in enumerate(orders) if "plan_amend" in order)
    assert orders[abort_at] == {"plan_amend": {"id": plan_id, "action": "abort"}}
    assert orders[abort_at + 1 :] == [], "a plan that already fired is left as it stands"


def test_a_rollback_waits_for_the_abort_to_publish_the_events_it_fired(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The abort is answered before the page republishes its mirror: the stale mirror shows no event fired and the deck
    idle, yet the incoming launch fired, so the deck is left as it stands rather than put back over the live mix."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-in", "playing": False, "position": {"ms": 5000.0}}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]
    page.abort_republish_lag = 3  # three reads still see the pre-abort snapshot
    page.fired_on_abort = 1

    def unconfirmed(plan_id: str) -> None:
        raise DjError("plan_unconfirmed", "the page did not publish the plan", plan_id=plan_id)

    monkeypatch.setattr(service, "_confirm_scheduled", unconfirmed)
    with pytest.raises(DjError):
        service.schedule(plan)
    orders = page.orders
    abort_at = next(i for i, order in enumerate(orders) if "plan_amend" in order)
    assert orders[abort_at + 1 :] == [], "the deck the aborted plan fired on is not put back"


def test_a_rolled_back_deck_gets_its_cue_back_before_its_playhead(requests: list[str]) -> None:
    """A load clears the cue, and a cue press on a paused deck stores the playhead as the cue. The cue is therefore set by
    seeking to the held cue and pressing cue, and the playhead the deck had is sought last."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {
        "stable_id": "trk-old",
        "playing": False,
        "cue_ms": 12000.0,
        "position": {"ms": 61000.0},
    }
    service = _service(page, requests)
    held = _held_deck(page.mirror_doc, 2)
    assert held is not None and held["cue_ms"] == 12000.0

    service._restore_deck(2, held)

    assert page.orders == [
        {"single": {"type": "load", "deck": 2, "stable_id": "trk-old"}},
        {"single": {"type": "seek", "deck": 2, "position_ms": 12000.0}},
        {"single": {"type": "cue", "deck": 2}},
        {"single": {"type": "seek", "deck": 2, "position_ms": 61000.0}},
    ]


def test_a_deck_with_no_cue_is_restored_without_a_cue_press(requests: list[str]) -> None:
    """A deck that never had a cue set is not given one: pressing cue would store its playhead as a cue it never had."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-old", "playing": False, "position": {"ms": 61000.0}}
    service = _service(page, requests)

    held = _held_deck(page.mirror_doc, 2)
    assert held is not None
    service._restore_deck(2, held)

    assert all(order["single"]["type"] != "cue" for order in page.orders)


def test_a_failed_prepare_is_rolled_back_though_its_failed_mirror_publishes_after_the_abort_is_refused(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page refuses the abort of a plan whose prepare failed, and publishes the failure afterwards: the first mirror
    still shows no failed plan. The rollback waits for the failed mirror, sees no event fired, and puts the deck back."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-old", "playing": False, "position": {"ms": 61000.0}}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def failed_prepare(order: dict[str, Any], plan_id: str) -> None:
        def publish_failed() -> None:
            page.mirror_doc["agent_plan"] = {"id": plan_id, "status": "failed", "fired": 0}

        page._abort_publication = publish_failed  # the failure lands after the page has answered the abort
        page._abort_lag_left = 3
        raise DjError("prepare_failed", "a prepare command failed", plan_id=plan_id)

    def abort_refused(plan_id: str, action: str, bars: int | None = None) -> Any:
        raise DjError("amend_refused", f"plan {plan_id} is already failed", plan_id=plan_id)

    monkeypatch.setattr(service, "_submit_plan", failed_prepare)
    monkeypatch.setattr(service, "amend", abort_refused)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "prepare_failed"
    assert {"single": {"type": "load", "deck": 2, "stable_id": "trk-old"}} in page.orders, "the deck is put back"


def test_a_refused_muted_schedule_puts_back_the_master_mute_as_it_was_read(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the operator had the master unmuted and a muted session's schedule mutes it, then a prepare fails [then] the
    rollback puts the master back unmuted, never persisted, [else stop]."""
    page = FakePage()
    page.mirror_doc["master"] = {"muted": False}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def muted_then_failed(order: dict[str, Any], plan_id: str) -> None:
        service._order({"single": _MUTE})  # the page applies the plan's own mute before its prepare fails
        page.mirror_doc["agent_plan"] = {
            "id": plan_id,
            "status": "aborted",
            "fired": 0,
        }  # the abort is published, nothing fired
        raise DjError("prepare_failed", "a prepare command failed", plan_id=plan_id)

    monkeypatch.setattr(service, "_submit_plan", muted_then_failed)
    monkeypatch.setattr(service, "amend", lambda plan_id, action, bars=None: None)
    with pytest.raises(DjError):
        service.schedule(plan)
    assert page.orders[-1] == {"single": {"type": "master_mute", "muted": False, "persist": False}}, (
        "the rollback restores the master mute as it was read, and never persists it"
    )


def test_a_track_loaded_onto_an_empty_deck_is_unloaded_when_the_plan_is_refused(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty incoming deck the schedule loaded goes back to empty: nothing is left behind as the track it did not hold."""
    page = FakePage()
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def loaded(deck: int, stable_id: str) -> dict[str, Any]:
        page.mirror_doc["decks"]["2"] = {"stable_id": stable_id, "playing": False, "position": {"ms": 0.0}}
        return page.mirror_doc

    def refused(order: Any, plan_id: str) -> Any:
        raise DjError("order_failed", "the page refused the plan")

    monkeypatch.setattr(service, "_confirm_loaded", loaded)
    monkeypatch.setattr(service, "_submit_plan", refused)
    with pytest.raises(DjError):
        service.schedule(plan)
    assert {"single": {"type": "unload", "deck": 2}} in page.orders


def test_a_plan_that_failed_before_it_fired_is_rolled_back_though_its_abort_is_refused(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan the page keeps as failed, with no event fired, was never armed: the refused abort does not block the rollback."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-in", "playing": False, "position": {"ms": 5000.0}}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def prepare_failed(order: Any, plan_id: str) -> Any:
        page.mirror_doc["agent_plan"] = {"id": plan_id, "status": "failed", "fired": 0}
        raise DjError("order_failed", "the page failed the plan's prepare")

    def abort_refused(plan_id: str, action: str, bars: int | None = None) -> Any:
        raise DjError("order_failed", f"plan {plan_id} is failed")

    monkeypatch.setattr(service, "_submit_plan", prepare_failed)
    monkeypatch.setattr(service, "amend", abort_refused)
    with pytest.raises(DjError):
        service.schedule(plan)
    assert any(order.get("single", {}).get("type") == "seek" for order in page.orders), "the deck is put back"


@pytest.mark.requirement("AUTOPLAN-05")
def test_a_plan_schedules_while_autoplay_is_on_because_autoplay_yields(requests: list[str]) -> None:
    """[if] AutoPlay is on with no handoff in flight [then] the plan is scheduled: AutoPlay stands aside for it, [else stop]."""
    page = FakePage()
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]
    page.mirror_doc.update(autoplay_enabled=True, autoplay_armed=True, autoplay_handoff_in_flight=False)
    assert service.schedule(plan)["scheduled"] is True, "if AutoPlay being on still refuses a plan then broken"


@pytest.mark.requirement("AUTOPLAN-05")
def test_schedule_refuses_a_handoff_in_flight_before_any_load(requests: list[str]) -> None:
    """[if] a handoff is loading or starting a deck [then] the schedule is refused before it touches a deck, [else stop]."""
    page = FakePage()
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]
    page.mirror_doc.update(autoplay_enabled=True, autoplay_handoff_in_flight=True)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "autoplay_handoff"
    assert page.orders == [], "nothing is loaded or sent during a handoff"


@pytest.mark.requirement("AUTOPLAN-05")
def test_schedule_refuses_a_page_that_does_not_report_handoffs_while_autoplay_is_on(requests: list[str]) -> None:
    """[if] the mirror cannot say whether a handoff is in flight [then] AutoPlay on is still refused, unmeasured is not clear, [else stop]."""
    page = FakePage()
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]
    page.mirror_doc["autoplay_enabled"] = True
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "autoplay_handoff"


@pytest.mark.requirement("AUTOPLAN-05")
def test_a_handoff_starting_during_the_load_is_refused_and_the_load_is_undone(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a handoff starts during the slow load [then] the post-load mirror refuses the plan and the load is undone, [else stop]."""
    page = FakePage()
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]
    page.mirror_doc["autoplay_handoff_in_flight"] = False
    post_order = page.post_order

    def start_a_handoff_while_loading(order: dict[str, Any]) -> dict[str, Any]:
        result = post_order(order)
        if order.get("single", {}).get("type") == "load":
            page.mirror_doc["autoplay_handoff_in_flight"] = True
        return result

    monkeypatch.setattr(page, "post_order", start_a_handoff_while_loading)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "autoplay_handoff"
    assert not any("plan" in order for order in page.orders), "no plan is submitted during a handoff"
    assert {"single": {"type": "unload", "deck": 2}} in page.orders, "the track the refused schedule loaded is unloaded"


@pytest.mark.requirement("AUTOPLAN-06")
def test_autoplay_decision_reads_the_mirror_and_decide_now_posts_the_command(requests: list[str]) -> None:
    """[if] an agent asks AutoPlay to decide [then] it posts the button's command and reads the page's answer, [else stop]."""
    page = FakePage()
    service = _service(page, requests)
    page.mirror_doc["autoplay_decision"] = {"phase": "deciding"}
    assert service.autoplay_decision() == {"autoplay_decision": {"phase": "deciding"}}
    assert service.autoplay_decide_now() == {"autoplay_decision": {"phase": "deciding"}}
    assert {"single": {"type": "auto_play_decide_now"}} in page.orders, "if decide now sends nothing then broken"


@pytest.mark.requirement("AGENT-29")
def test_a_muted_session_cannot_finish_a_plan_now_but_a_live_one_can(requests: list[str]) -> None:
    """[if] a muted session asks finish_now [then] it is refused before the page plays the remaining steps at once, [else stop]."""
    page = FakePage()
    page.mirror_doc["agent_plan"] = {"id": "plan-x", "status": "scheduled", "fired": 0}
    with pytest.raises(DjError) as refused:
        _service(page, requests).amend("plan-x", "finish_now")
    assert refused.value.code == "amend_refused"
    assert page.orders == [], "a refused finish_now sends nothing to the page"
    _service(page, requests, audio="live").amend("plan-x", "finish_now")
    assert any("plan_amend" in order for order in page.orders), "a live session may finish the plan now"


def test_a_schedule_refused_for_a_handoff_exits_as_a_refusal() -> None:
    """[if] a schedule is refused because AutoPlay is handing off [then] the CLI exits with the refusal code, not a failure, [else stop]."""
    assert dj_cli._exit_for("autoplay_handoff") == EXIT_UNCONFIRMED
    assert dj_cli._exit_for("usage") != EXIT_UNCONFIRMED  # control: a code that is not a refusal keeps its own exit


@pytest.mark.requirement("AGENT-29")
def test_a_deck_the_operator_starts_during_a_refused_schedule_is_not_rolled_back(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the operator starts the incoming deck while the page refuses the plan [then] the rollback leaves it as it stands, [else stop]."""
    page = FakePage()
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]
    post_order = page.post_order

    def operator_starts_deck_and_plan_is_refused(order: dict[str, Any]) -> dict[str, Any]:
        if "plan" in order:
            page.mirror_doc["decks"]["2"]["playing"] = True
            page.refuse_plan = "the page refused the plan"
        return post_order(order)

    monkeypatch.setattr(page, "post_order", operator_starts_deck_and_plan_is_refused)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "plan_refused"
    plan_index = next(i for i, order in enumerate(page.orders) if "plan" in order)
    after_plan = page.orders[plan_index + 1 :]
    assert [o for o in after_plan if "plan_amend" not in o] == [], "nothing restores the deck the operator started"


def test_an_explicit_overlap_is_scored_when_both_start_bars_are_omitted(requests: list[str]) -> None:
    """Best-window scoring keeps the overlap asked for: the summary reports it, not the default."""
    service = _service(FakePage(), requests)
    summary = service.score_pair("trk-out", "trk-in", overlap_bars=4)
    assert summary["overlap_bars"] == 4


def test_an_explicit_overlap_must_be_a_positive_whole_number_of_bars(requests: list[str]) -> None:
    """A zero or negative overlap would be silently replaced or clamped, so the CLI and MCP surface refuses it."""
    service = _service(FakePage(), requests)
    bad_overlaps: tuple[Any, ...] = (0, -3, True, 2.5)
    for bad in bad_overlaps:
        with pytest.raises(DjError) as refused:
            service.score_pair("trk-out", "trk-in", out_start_bar=9, in_start_bar=9, overlap_bars=bad)
        assert refused.value.code == "usage"


def test_an_unknown_abort_outcome_leaves_the_deck_alone(requests: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A submit that raised may still have armed the plan: when the abort itself cannot be answered, nothing is put back."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-in", "playing": False, "position": {"ms": 5000.0}}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def submit_times_out(order: Any, plan_id: str) -> Any:
        raise DjError("order_failed", "the page did not answer the plan order")

    def abort_unanswered(plan_id: str, action: str, bars: int | None = None) -> Any:
        raise DjError("order_failed", "the page did not answer the abort")

    monkeypatch.setattr(service, "_submit_plan", submit_times_out)
    monkeypatch.setattr(service, "amend", abort_unanswered)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "order_failed"
    assert page.orders == [], "the deck is left as it stands while the outcome is unknown"


def test_a_plan_the_page_does_not_hold_is_rolled_back_after_a_refused_abort(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused submit is not armed, so the page has no such plan to abort: the deck is put back as usual."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-in", "playing": False, "position": {"ms": 5000.0}}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def refuse_the_submit(order: Any, plan_id: str) -> Any:
        raise DjError("plan_refused", "the page refused the plan")

    def no_such_plan(plan_id: str, action: str, bars: int | None = None) -> Any:
        raise DjError("amend_refused", f"Error: no plan {plan_id} on this page", plan_id=plan_id)

    monkeypatch.setattr(service, "_submit_plan", refuse_the_submit)
    monkeypatch.setattr(service, "amend", no_such_plan)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "plan_refused"
    assert [order["single"] for order in page.orders] == [  # no pitch on this mirror, so no tempo is put back
        {"type": "seek", "deck": 2, "position_ms": 5000.0},
        {"type": "fader", "deck": 2, "value": 1.0},
    ]


def test_schedule_refuses_an_armed_deck_even_when_it_holds_the_track(requests: list[str]) -> None:
    """A deck armed for a launch is never reused, even for the track it already holds: a seek or tempo would change that launch."""
    page = FakePage()
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-in", "playing": False, "position": {"ms": 5000.0}}
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]
    page.mirror_doc["decks"]["2"]["quantized_launch_armed"] = {"deck": 2}  # armed after the proposal
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "deck_busy"
    assert page.orders == [], "nothing is sent to an armed deck"


def test_a_refused_abort_for_another_reason_keeps_the_deck_as_it_is(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the runner's "no plan" answer means the plan is not armed; any other refusal leaves the deck untouched."""
    page = FakePage()
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]

    def accepted_but_unpublished(plan_id: str) -> None:
        raise DjError("plan_unconfirmed", "the page accepted the plan but has not published it", plan_id=plan_id)

    def abort_refused_otherwise(plan_id: str, action: str, bars: int | None = None) -> Any:
        raise DjError("amend_refused", "Error: plan is already done", plan_id=plan_id)

    monkeypatch.setattr(service, "_confirm_scheduled", accepted_but_unpublished)
    monkeypatch.setattr(service, "amend", abort_refused_otherwise)
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "amend_refused"
    assert [order["single"] for order in page.orders if "single" in order] == [
        {"type": "load", "deck": 2, "stable_id": "trk-in"},
    ], "the rollback is skipped: nothing is put back"


def test_validate_needs_a_page_unless_offline_is_asked_for(requests: list[str]) -> None:
    """A missing page is an error for a live validation; the offline check is the explicit ``live=False``."""
    page = FakePage()
    service = _service(page, requests)
    plan = service.propose("trk-in")["proposals"][0]["plan"]
    page.page_open = False
    with pytest.raises(DjError) as missing:
        service.validate(plan)
    assert missing.value.code == "no_performance_page"
    assert "ok" in service.validate(plan, live=False)


def test_cli_verbs_is_the_static_catalog_with_no_engine(
    requests: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """``opendj dj verbs`` answers the same document as the MCP list_verbs tool, without a page."""
    page = FakePage()
    code, verbs = _cli(page, requests, capsys, "verbs")
    assert code == EXIT_CONFIRMED
    assert verbs["verbs"], "the catalog lists the primitives"
    assert page.orders == [] and page.mirror_reads == 0


def test_schedule_refuses_a_busy_incoming_deck(requests: list[str]) -> None:
    page = FakePage()
    service = _service(page, requests)
    plan = service.propose("trk-in", in_deck=2)["proposals"][0]["plan"]  # a valid plan while deck 2 is idle
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-next", "playing": True}  # then deck 2 starts playing
    with pytest.raises(DjError) as busy:
        service.schedule(plan)
    assert busy.value.code == "deck_busy"
    assert page.orders == []


def test_schedule_refuses_a_busy_page_before_it_loads_the_incoming_deck(requests: list[str]) -> None:
    page = FakePage()
    page.mirror_doc["agent_plan"] = {"id": "plan-old", "status": "running"}
    service = _service(page, requests)
    plan = service.propose("trk-in")["proposals"][0]["plan"]
    with pytest.raises(DjError) as busy:
        service.schedule(plan)
    assert busy.value.code == "plan_busy" and "plan-old is running" in str(busy.value)
    # Control: once the running plan is over, the same schedule loads and posts.
    page.mirror_doc["agent_plan"] = {"id": "plan-old", "status": "done"}
    assert service.schedule(plan)["scheduled"] is True
    assert any(order.get("single", {}).get("type") == "load" for order in page.orders)


def test_schedule_surfaces_a_page_refusal(requests: list[str]) -> None:
    page = FakePage()
    page.refuse_plan = "plan_busy: plan plan-old is running"
    service = _service(page, requests)
    plan = service.propose("trk-in")["proposals"][0]["plan"]
    with pytest.raises(DjError) as refused:
        service.schedule(plan)
    assert refused.value.code == "plan_refused" and "plan_busy" in str(refused.value)


def test_next_phrase_anchor_resolves_to_a_section_start(requests: list[str]) -> None:
    service = _service(FakePage(out_bar=100.0), requests)
    plan = service.propose("trk-in")["proposals"][0]["plan"]
    plan["anchor"] = "next_phrase"
    plan["out"]["window"]["start_bar"] = 1
    validated = service.validate(plan)
    assert validated["plan"]["out"]["window"]["start_bar"] == 113  # the outro
    assert validated["plan"]["anchor"] == "window"  # resolved, so the page gets a concrete bar


def test_amend_validates_the_action_and_posts_plan_amend(requests: list[str]) -> None:
    page = FakePage()
    service = _service(page, requests)
    with pytest.raises(DjError) as usage:
        service.amend("plan-x", "hold")
    assert usage.value.code == "usage"
    with pytest.raises(DjError):
        service.amend("plan-x", "extend")
    page.mirror_doc["agent_plan"] = {"id": "plan-x", "status": "running", "fired": 0}  # the page holds the plan
    result = service.amend("plan-x", "abort")
    assert page.orders == [{"plan_amend": {"id": "plan-x", "action": "abort"}}]
    assert result["page"]["status"] == "aborted"


def test_no_page_is_a_named_error(requests: list[str]) -> None:
    with pytest.raises(DjError) as closed:
        _service(FakePage(page_open=False), requests).mix_state()
    assert closed.value.code == "no_performance_page"


# ----- opendj dj ------------------------------------------------------------


def _cli(
    page: FakePage, requests: list[str], capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, dict[str, Any]]:
    code = dj_cli.run(list(argv), lock=None, service_factory=lambda lock: _service(page, requests))
    return code, json.loads(capsys.readouterr().out)


def test_cli_state_and_styles(requests: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, state = _cli(FakePage(), requests, capsys, "state")
    assert code == EXIT_CONFIRMED and state["decks"]["1"]["section"]["label"] == "drop"
    code, styles = _cli(FakePage(), requests, capsys, "styles")
    assert code == EXIT_CONFIRMED and {row["name"] for row in styles["styles"]} >= {"cut", "long_blend"}
    assert any((row.get("params") or {}).get("fade_shape") == "smooth" for row in styles["styles"])


def test_cli_propose_then_schedule_from_a_file(
    requests: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    page = FakePage()
    code, proposed = _cli(page, requests, capsys, "propose", "trk-in", "--intent", "wind down")
    assert code == EXIT_CONFIRMED and proposed["intent"] == "wind_down"
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(proposed["proposals"][0]["plan"]), encoding="utf-8")
    code, scheduled = _cli(page, requests, capsys, "schedule", str(plan_file))
    assert code == EXIT_CONFIRMED and scheduled["scheduled"] is True


def test_cli_exits_unconfirmed_when_the_page_never_shows_the_load(
    requests: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.opendj_cli import dj_service

    monkeypatch.setattr(dj_service, "LOAD_CONFIRM_S", 0.2)
    plan = _service(FakePage(), []).propose("trk-in")["proposals"][0]["plan"]
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    page = FakePage(out_bar=100.0)
    page.republish_lag = None
    code, error = _cli(page, requests, capsys, "schedule", str(plan_file))
    assert code == EXIT_UNCONFIRMED and error["error"] == "load_unconfirmed"
    # Control: the same schedule on a page that confirms the load exits confirmed.
    code, scheduled = _cli(FakePage(out_bar=100.0), requests, capsys, "schedule", str(plan_file))
    assert code == EXIT_CONFIRMED and scheduled["scheduled"] is True


def test_cli_exit_codes(requests: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, error = _cli(FakePage(), requests, capsys, "windows", "trk-in")
    assert code == EXIT_FAILED and error["error"] == "usage"
    code, error = _cli(FakePage(page_open=False), requests, capsys, "state")
    assert code == EXIT_NO_PAGE and error["error"] == "no_performance_page"
    page = FakePage(out_playing=False)
    plan = _service(FakePage(), []).propose("trk-in")["proposals"][0]["plan"]
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    code, error = _cli(page, requests, capsys, "schedule", str(plan_file))
    assert code == EXIT_UNCONFIRMED and error["error"] == "plan_invalid"
    code, error = _cli(page, requests, capsys, "validate", str(tmp_path / "missing.json"))
    assert code == EXIT_FAILED and "cannot read plan" in error["message"]


@pytest.mark.requirement("AGENT-26")
def test_cli_schedules_a_saved_mix_score_and_shows_it_offline(
    requests: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """[if] a proposal is saved as a .mix file [then] show prints it and schedule plays it, [else stop]."""
    page = FakePage()
    code, proposed = _cli(page, requests, capsys, "propose", "trk-in")
    assert code == EXIT_CONFIRMED
    score_file = tmp_path / "blend.mix"
    score_file.write_text(proposed["proposals"][0]["notation"], encoding="utf-8")
    assert dj_cli.run(["show", str(score_file), "--as", "grid"], lock=None) == EXIT_CONFIRMED
    grid = capsys.readouterr().out
    assert grid.startswith("bar") and "D2 deck" in grid
    wide_file = tmp_path / "wide.mix"
    wide_file.write_text("transition cut\nout D1 a from 1 for 1000000\nin D2 b from 1 for 4\n@0 D2 play\n", encoding="utf-8")
    assert dj_cli.run(["show", str(wide_file), "--as", "grid"], lock=None) == EXIT_UNCONFIRMED
    assert "more than" in json.loads(capsys.readouterr().out)["message"]
    code, scheduled = _cli(page, requests, capsys, "schedule", str(score_file))
    assert code == EXIT_CONFIRMED and scheduled["plan"] == proposed["proposals"][0]["plan"]
    score_file.write_text("transition cut\nout D1 a from 1 for 4\n", encoding="utf-8")
    code, error = _cli(page, requests, capsys, "validate", str(score_file))
    assert code == EXIT_FAILED and "is not a mix score" in error["message"]



# ----- sets (AGENT-27) ------------------------------------------------------

SET_IDS = ["trk-out", "trk-in", "trk-next"]


@pytest.mark.requirement("AGENT-27")
def test_plan_set_starts_from_the_live_deck_and_bar(requests: list[str]) -> None:
    """[if] the first track is playing [then] the set starts on its deck after its bar, [else stop]."""
    planned = _service(FakePage(out_bar=100.0), requests).plan_set(SET_IDS, intent="smooth", name="warmup")
    first = planned["set"]["transitions"][0]
    assert first["out"]["deck"] == 1 and first["out"]["window"]["start_bar"] > 100
    assert [row["deck"] for row in planned["set"]["tracks"]] == [1, 2, 1]
    assert planned["score"].startswith("set warmup\n")
    # Control: with no page open it still plans, from bar 1 on deck 1, and says so.
    offline = _service(FakePage(page_open=False), requests).plan_set(SET_IDS)
    assert offline["notes"] and "without the live mix" in offline["notes"][0]
    assert offline["set"]["transitions"][0]["out"]["window"]["start_bar"] <= 100


@pytest.mark.requirement("AGENT-27")
def test_plan_set_first_deck_follows_the_live_deck_or_is_refused(requests: list[str]) -> None:
    """[if] first_deck conflicts with the playing deck, or is 0 [then] plan_set refuses, [else stop]."""
    live = _service(FakePage(out_bar=100.0), requests)
    with pytest.raises(DjError, match=r"playing on D1.*first_deck 3 conflicts"):
        live.plan_set(SET_IDS, first_deck=3)
    assert live.plan_set(SET_IDS, first_deck=1)["set"]["tracks"][0]["deck"] == 1  # control: agreeing is fine
    offline = _service(FakePage(page_open=False), requests)
    with pytest.raises(DjError, match=r"deck 1\.\.4"):
        offline.plan_set(SET_IDS, first_deck=0)
    assert offline.plan_set(SET_IDS, first_deck=3)["set"]["tracks"][0]["deck"] == 3  # control: an offline override holds


@pytest.mark.requirement("AGENT-27")
def test_plan_set_falls_back_offline_only_without_a_page(requests: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] reading the mix fails but not for a missing page [then] plan_set raises it, [else stop]."""
    service = _service(FakePage(), requests)

    def broken() -> dict[str, Any]:
        raise DjError("order_failed", "the engine answered garbage")

    monkeypatch.setattr(service, "_mirror", broken)
    with pytest.raises(DjError, match="garbage"):
        service.plan_set(SET_IDS)


def _swap_out_track_to_d2(page: FakePage) -> None:
    decks = page.mirror_doc["decks"]
    decks["1"], decks["2"] = decks["2"], decks["1"]


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize(
    ("label", "mutate", "message"),
    [
        ("decks list", lambda page: page.mirror_doc.__setitem__("decks", []), "no decks object"),
        ("deck key x", lambda page: page.mirror_doc["decks"].__setitem__("x", page.mirror_doc["decks"].pop("4")), "unknown deck 'x'"),
        ("no stable_id", lambda page: page.mirror_doc["decks"]["3"].pop("stable_id"), "deck 3 has no stable_id"),
        ("ms true", lambda page: page.mirror_doc["decks"]["1"]["position"].__setitem__("ms", True), "position.ms"),
        ("ms nan", lambda page: page.mirror_doc["decks"]["1"]["position"].__setitem__("ms", float("nan")), "position.ms"),
        ("agent_plan armed", lambda page: page.mirror_doc.__setitem__("agent_plan", {"status": "armed"}), "agent_plan"),
    ],
)
def test_plan_set_refuses_a_malformed_mirror(requests: list[str], label: str, mutate: Any, message: str) -> None:
    """[if] an open page's mirror fails set-next's checks [then] plan_set is mirror_invalid, [else stop]."""
    page = FakePage(out_bar=100.0)
    mutate(page)
    with pytest.raises(DjError, match=message) as refused:
        _service(page, requests).plan_set(SET_IDS)
    assert refused.value.code == "mirror_invalid", label
    # Control: the untouched mirror plans live, with no note.
    live = _service(FakePage(out_bar=100.0), requests).plan_set(SET_IDS)
    assert "notes" not in live and live["set"]["transitions"][0]["out"]["window"]["start_bar"] > 100


@pytest.mark.requirement("AGENT-27")
def test_plan_set_says_when_it_did_not_start_from_the_live_mix(requests: list[str]) -> None:
    """[if] the first track is paused or unplaced [then] plan_set starts at bar 1, [else stop]."""
    paused = FakePage(out_bar=100.0, out_playing=False)
    _swap_out_track_to_d2(paused)
    planned = _service(paused, requests).plan_set(SET_IDS)
    assert planned["notes"] == ["planned without the live mix: trk-out is loaded on D2 but not playing, so the set starts from bar 1"]
    assert planned["set"]["tracks"][0]["deck"] == 1
    no_position = FakePage(out_bar=100.0)
    _swap_out_track_to_d2(no_position)
    no_position.mirror_doc["decks"]["2"]["position"] = {"ms": None}
    planned = _service(no_position, requests).plan_set(SET_IDS)
    assert "D2 plays trk-out but publishes no position" in planned["notes"][0]
    assert planned["set"]["tracks"][0]["deck"] == 2  # the deck is known even without the bar
    absent = FakePage(out_bar=100.0)
    absent.mirror_doc["decks"]["1"]["stable_id"] = None
    assert "trk-out is not on any deck" in _service(absent, requests).plan_set(SET_IDS)["notes"][0]
    # Control: playing with a position plans live on its deck with no note.
    live = FakePage(out_bar=100.0)
    _swap_out_track_to_d2(live)
    planned = _service(live, requests).plan_set(SET_IDS)
    assert "notes" not in planned and planned["set"]["tracks"][0]["deck"] == 2


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize("first_deck", [True, 2.0, 0, 5])
def test_plan_set_refuses_a_bad_first_deck_as_usage(requests: list[str], first_deck: Any) -> None:
    """[if] first_deck is not an integer deck 1..4 [then] plan_set refuses it as usage, [else stop]."""
    offline = _service(FakePage(page_open=False), requests)
    with pytest.raises(DjError, match=r"first_deck must be a deck 1\.\.4") as refused:
        offline.plan_set(SET_IDS, first_deck=first_deck)
    assert refused.value.code == "usage"
    # Control: an integer deck holds.
    assert offline.plan_set(SET_IDS, first_deck=2)["set"]["tracks"][0]["deck"] == 2


@pytest.mark.requirement("AGENT-27")
def test_plan_set_refuses_a_repeated_track_as_usage(requests: list[str]) -> None:
    """[if] the running order names a track twice [then] plan_set refuses it as usage, [else stop]."""
    service = _service(FakePage(out_bar=100.0), requests)
    for order in (["trk-out", "trk-in", "trk-out"], ["trk-out", "trk-out", "trk-in"]):
        with pytest.raises(DjError, match="trk-out appears twice") as refused:
            service.plan_set(order)
        assert refused.value.code == "usage"
    # Control: distinct tracks plan.
    assert len(service.plan_set(SET_IDS)["set"]["transitions"]) == 2


@pytest.mark.requirement("AGENT-27")
def test_schedule_set_next_performs_only_the_due_transition(requests: list[str]) -> None:
    """[if] a set is handed over mid-track [then] only the due transition is scheduled, [else stop]."""
    page = FakePage(out_bar=60.0)
    service = _service(page, requests, audio="live")
    from apps.autodj.setscore import parse_set

    planned = parse_set(service.plan_set(SET_IDS)["set"])
    result = service.schedule_set_next(planned)
    assert result["scheduled"] is True and result["set_index"] == 0 and result["transitions"] == 2
    plan_orders = [order["plan"] for order in page.orders if "plan" in order]
    assert len(plan_orders) == 1 and plan_orders[0]["incoming"] == {"deck": 2, "stable_id": "trk-in"}
    page.mirror_doc["agent_plan"] = {"status": "running"}
    busy = service.schedule_set_next(planned)
    assert busy == {"scheduled": False, "reason": busy["reason"], "transitions": 2} and "running" in busy["reason"]
    assert len([order for order in page.orders if "plan" in order]) == 1  # nothing more armed


@pytest.mark.requirement("AGENT-27")
def test_schedule_set_next_decides_and_schedules_on_one_mirror_read(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] set-next checks what is due [then] it schedules on that same snapshot, read once, [else stop].

    Control: a read between the due check and the schedule would let a track that started on another deck
    in between be loaded twice. Control two: the handover is still scheduled."""
    page = FakePage(out_bar=60.0)
    page.mirror_doc["decks"]["2"] = {"stable_id": "trk-in", "playing": False}  # loaded: no load, so no confirm read
    service = _service(page, requests, audio="live")
    from apps.autodj.setscore import parse_set

    planned = parse_set(service.plan_set(SET_IDS)["set"])
    reads = 0
    read = service._mirror

    def counted() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return read()

    # The decision is one mirror read; schedule() then does its own load and schedule confirmation reads.
    reads_at_schedule: list[int] = []
    schedule = service.schedule

    def recorded(plan: Any, mirror: Any = None) -> dict[str, Any]:
        reads_at_schedule.append(reads)
        return schedule(plan, mirror)

    monkeypatch.setattr(service, "_mirror", counted)
    monkeypatch.setattr(service, "schedule", recorded)
    result = service.schedule_set_next(planned)
    assert result["scheduled"] is True and result["set_index"] == 0
    assert reads_at_schedule == [1]


@pytest.mark.requirement("AGENT-27")
def test_schedule_set_next_refuses_a_malformed_mirror(requests: list[str]) -> None:
    """[if] the mirror lacks a deck the set uses [then] set-next is mirror_invalid, [else stop]."""
    page = FakePage(out_bar=60.0)
    service = _service(page, requests, audio="live")
    from apps.autodj.setscore import parse_set

    planned = parse_set(service.plan_set(SET_IDS)["set"])
    del page.mirror_doc["decks"]["2"]
    with pytest.raises(DjError, match="no deck 2") as refused:
        service.schedule_set_next(planned)
    assert refused.value.code == "mirror_invalid"
    assert not [order for order in page.orders if "plan" in order]


@pytest.mark.requirement("AGENT-27")
def test_cli_plans_a_set_shows_it_offline_and_performs_the_next_step(
    requests: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """[if] set-plan output is saved [then] set-show prints it and set-next schedules it, [else stop]."""
    page = FakePage(out_bar=60.0)
    code, planned = _cli(page, requests, capsys, "set-plan", *SET_IDS, "--intent", "wind down")
    assert code == EXIT_CONFIRMED
    set_file = tmp_path / "set.mix"
    set_file.write_text(planned["score"], encoding="utf-8")
    assert dj_cli.run(["set-show", str(set_file), "--as", "grid"], lock=None) == EXIT_CONFIRMED
    grids = capsys.readouterr().out
    assert grids.count("\nD2 deck") + grids.count("\nD1 deck") >= 4  # two grids, two deck lanes each
    wide = json.loads(json.dumps(planned["set"]))
    for side in ("out", "in"):
        wide["transitions"][-1][side]["window"]["bars"] = 10**9
    wide_file = tmp_path / "wide-set.json"
    wide_file.write_text(json.dumps(wide), encoding="utf-8")
    assert dj_cli.run(["set-show", str(wide_file), "--as", "grid"], lock=None) == EXIT_UNCONFIRMED
    assert json.loads(capsys.readouterr().out)["error"] == "plan_invalid"
    # A window too wide to read back as a score is plan_invalid in the score view too.
    wide["transitions"][-1]["in"]["window"]["bars"] = 10**30
    wide_file.write_text(json.dumps(wide), encoding="utf-8")
    assert dj_cli.run(["set-show", str(wide_file), "--as", "score"], lock=None) == EXIT_UNCONFIRMED
    assert "30 digits" in json.loads(capsys.readouterr().out)["message"]
    # Deeply nested JSON names it: a usage error for a set, plan_invalid for a plan (AGENT-5858).
    deep_file = tmp_path / "deep.json"
    deep_file.write_text('{"transitions": ' + "[" * 100_000 + "]" * 100_000 + "}", encoding="utf-8")
    for tool, exit_code in (("set-show", EXIT_FAILED), ("show", EXIT_UNCONFIRMED)):
        assert dj_cli.run([tool, str(deep_file)], lock=None) == exit_code
        assert "nested too deeply" in json.loads(capsys.readouterr().out)["message"]
    code, scheduled = _cli(page, requests, capsys, "set-next", str(set_file))
    assert code == EXIT_CONFIRMED and scheduled["set_index"] == 0
    page.mirror_doc["decks"]["1"]["playing"] = False
    code, idle = _cli(page, requests, capsys, "set-next", str(set_file))
    assert code == EXIT_UNCONFIRMED and idle["scheduled"] is False
    set_file.write_text(re.sub(r" from \d+ for", " from 0 for", planned["score"], count=1), encoding="utf-8")
    code, error = _cli(page, requests, capsys, "set-next", str(set_file))
    assert code == EXIT_FAILED and "is not a valid set" in error["message"]


@pytest.mark.requirement("AGENT-29")
def test_the_incoming_deck_skips_a_deck_armed_for_a_launch() -> None:
    """[if] the preferred partner is armed for a quantized launch [then] the idle deck is picked, [else stop]."""
    from apps.opendj_cli.dj_service import _pick_in_deck

    armed = {"2": {"playing": False, "quantized_launch_armed": {"bar": 9}}, "3": {"playing": False}}
    assert _pick_in_deck(1, armed, "trk-in") == 3
    # Control: with nothing armed, the partner is still the first choice.
    assert _pick_in_deck(1, {"2": {"playing": False}, "3": {"playing": False}}, "trk-in") == 2
    # An armed deck holding the track is not reused either: the idle deck takes the load instead.
    holds_armed = {
        "2": {"stable_id": "trk-in", "playing": False, "quantized_launch_armed": {"bar": 9}},
        "3": {"playing": False},
    }
    assert _pick_in_deck(1, holds_armed, "trk-in") == 3
    # A playing deck holding the track is not reused either, but a paused one still is (control).
    holds_playing = {"2": {"stable_id": "trk-in", "playing": True}, "3": {"playing": False}}
    assert _pick_in_deck(1, holds_playing, "trk-in") == 3
    assert _pick_in_deck(1, {"2": {"stable_id": "trk-in", "playing": False}, "3": {"playing": False}}, "trk-in") == 2
    # A load over a deck armed for another track is refused before the load is sent.
    from apps.opendj_cli.dj_service import _needs_load

    with pytest.raises(DjError) as armed:
        _needs_load(
            {"decks": {"3": {"stable_id": "trk-old", "playing": False, "quantized_launch_armed": {"bar": 9}}}},
            3,
            "trk-in",
        )
    assert armed.value.code == "deck_busy"
    assert _needs_load({"decks": {"3": {"stable_id": "trk-old", "playing": False}}}, 3, "trk-in") is True


def test_the_projected_incoming_deck_takes_the_incoming_tempo_not_the_previous_track_s() -> None:
    """Validation reads the projected deck's tempo; a stale BPM from the previous track would miscount the grid."""
    from apps.opendj_cli.dj_service import _with_in_track

    mirror = {"decks": {"2": {"stable_id": "old", "playing": False, "bpm": 140.0, "effective_bpm": 140.0}}}
    projected = _with_in_track(mirror, 2, "new", 100.0)
    assert projected["decks"]["2"]["stable_id"] == "new"
    assert projected["decks"]["2"]["effective_bpm"] == 100.0
    assert mirror["decks"]["2"]["effective_bpm"] == 140.0  # the input mirror is not mutated
    playing = {"decks": {"2": {"stable_id": "old", "playing": True, "effective_bpm": 140.0}}}
    assert (
        _with_in_track(playing, 2, "new", 100.0)["decks"]["2"]["effective_bpm"] == 140.0
    )  # control: a playing deck is left alone


@pytest.mark.parametrize("style_name", ["bass_swap_blend", "crossfade"])
@pytest.mark.parametrize("out_bar", [100.0, 100.25, 100.5, 100.75, 100.99])
def test_a_next_bar_anchor_leaves_room_for_the_plan_pre_roll(
    requests: list[str], style_name: str, out_bar: float
) -> None:
    """AGENT-35 (Codex P2/BLOCKING on #7595): a compensated blend starts its outgoing ease at bar -1, so next_bar
    resolves far enough ahead for that pre-roll at any point in the bar; a crossfade (no pre-roll) is the control."""
    service = _service(FakePage(out_bar=out_bar), requests)
    # PR #12799 (AUTODJ-11851): bass placement moves this fixture's blend entry into a 7-bar in-window, which blends
    # now refuse. Without bass analysis the full 16-bar window stays mixable, as that PR's own propose tests do.
    service._profiles["trk-in"] = replace(service.profile("trk-in"), bar_bass=None)
    proposals = service.propose("trk-in", top=50)["proposals"]
    plan = next(p["plan"] for p in proposals if p["style"] == style_name)
    plan["anchor"] = "next_bar"
    validated = service.validate(plan)
    assert not [e for e in validated["errors"] if "already at bar" in e], validated["errors"]


def _propose_with_mirror_changed(
    change: Callable[[dict[str, Any]], None], requests: list[str], start_bar: float = 100.0
) -> dict[str, Any]:
    """Propose while the mirror changes during the search: every read after the first sees ``change`` applied."""
    page = FakePage(out_bar=start_bar)
    first_read = page.mirror

    def changing_mirror() -> dict[str, Any]:
        doc = first_read()
        if page.mirror_reads >= 2:
            doc = copy.deepcopy(doc)
            change(doc)
        return doc

    page.mirror = changing_mirror  # type: ignore[method-assign]
    return _service(page, requests).propose("trk-in", intent="smooth")


def _propose_with_deck_moved_to(bar: float, requests: list[str], start_bar: float = 100.0) -> dict[str, Any]:
    """Propose while the outgoing deck reaches ``bar`` during the search (from ``start_bar`` on the first read)."""
    return _propose_with_mirror_changed(
        lambda doc: doc["decks"]["1"].__setitem__("position", {"ms": OUT.time_of_bar(bar) * 1000.0}),
        requests,
        start_bar,
    )


#: A playhead with three outgoing windows still ahead (bars 65, 81 and 113): the top-ranked candidates include an early
#: window, so lower-ranked ones start later and can backfill. Since D8 no style pre-rolls a bar before its window, so
#: candidates in one window all share one first command and nothing could backfill from them.
WINDOWS_AHEAD = 60.0


def test_propose_rereads_the_playhead_and_drops_plans_whose_start_passed(requests: list[str]) -> None:
    """[if] the outgoing deck moves on while proposals are computed [then] a plan whose first event is no longer more
    than the schedule margin ahead is dropped, and none left is window_passed, [else stop]. Codex #7595."""
    from apps.autodj.plan import first_dispatch_bar, parse_plan

    still = _service(FakePage(out_bar=100.0), requests).propose("trk-in", intent="smooth")
    assert still["proposals"] and still["dropped"] == []
    # The first COMMAND, as the anchor lead checks it: a play at bar 0 is dispatched a quarter bar ahead.
    first = min(
        float(row["plan"]["out"]["window"]["start_bar"]) + first_dispatch_bar(parse_plan(row["plan"]))
        for row in still["proposals"]
    )
    # Control: the deck moved, but the earliest plan is still just over the anchor lead ahead, so nothing is dropped.
    near = _propose_with_deck_moved_to(first - 1.1, requests)
    assert near["dropped"] == [] and near["out_bar_now"] == pytest.approx(first - 1.1, abs=0.01)
    with pytest.raises(DjError) as passed:  # past every candidate's start: none is left to backfill
        _propose_with_deck_moved_to(first + 40.0, requests)
    assert passed.value.code == "proposals_invalidated" and "already at bar" in str(passed.value)


def test_propose_never_returns_a_plan_whose_pre_roll_is_behind_the_live_bar(requests: list[str]) -> None:
    """[if] a style starts with a pre-roll event before its window [then] the plan's FIRST event, not its window, is
    after the live bar plus the anchor lead, [else stop]. Codex #7595: those plans were returned already unschedulable."""
    for out_bar in (
        100.0,
        110.5,
        111.2,
    ):  # at 111.2 the bar 113 window passes the window filter, its bar 112 pre-roll does not
        result = _service(FakePage(out_bar=out_bar), requests).propose("trk-in", intent="smooth")
        for row in result["proposals"]:
            first = float(row["plan"]["out"]["window"]["start_bar"]) + min(e["at_bar"] for e in row["plan"]["events"])
            assert first > out_bar + 1.0, (out_bar, row["style"], first)


def test_propose_with_no_window_left_answers_empty_not_window_passed(requests: list[str]) -> None:
    """Control for the re-read: a search that found nothing is an empty answer, not a plan whose start passed."""
    result = _service(FakePage(out_bar=118.0), requests).propose("trk-in", intent="smooth")
    assert result["proposals"] == [] and result["dropped"] == []


@pytest.mark.parametrize(
    ("level", "change"),
    [
        ("decks null", lambda doc: doc.__setitem__("decks", None)),
        ("decks a list", lambda doc: doc.__setitem__("decks", [])),
        ("deck null", lambda doc: doc["decks"].__setitem__("1", None)),
        ("deck a list", lambda doc: doc["decks"].__setitem__("1", [])),
    ],
)
def test_propose_refuses_a_malformed_mirror_on_the_final_reread(
    level: str, change: Callable[[dict[str, Any]], None], requests: list[str]
) -> None:
    """[if] the mirror read after the search has a null or non-object level [then] propose refuses it as
    mirror_malformed, never as a stale deck, [else stop]. Sol P1 #7595."""
    with pytest.raises(DjError) as malformed:
        _propose_with_mirror_changed(change, requests)
    assert malformed.value.code == "mirror_malformed", level


def test_propose_does_not_report_a_bad_proposal_plan_as_mirror_malformed(
    requests: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a generated proposal's plan fails parse_plan [then] propose raises the proposal-specific DjError
    plan_invalid, never mirror_malformed, [else stop]. Issue #9314 / review follow-up #7657, #13083."""
    from apps.opendj_cli import dj_service as dj_mod

    real = dj_mod.propose_transition

    def poison(*args: object, **kwargs: object) -> dict[str, Any]:
        result = real(*args, **kwargs)
        assert result["proposals"], "search must return a row so recheck hits parse_plan"
        first = {**result["proposals"][0], "plan": "not-a-plan"}
        return {**result, "proposals": [first, *result["proposals"][1:]]}

    monkeypatch.setattr(dj_mod, "propose_transition", poison)
    with pytest.raises(DjError) as raised:
        _service(FakePage(out_bar=100.0), requests).propose("trk-in", intent="smooth")
    assert raised.value.code == "plan_invalid"  # #13083: the proposal keeps its own classification


def test_propose_revalidates_candidates_against_the_fresh_mirror(requests: list[str]) -> None:
    """[if] the incoming deck starts another track during the search [then] every candidate is revalidated against the
    fresh mirror and dropped with its reason, [else stop]. Sol P1 #7595. Control: with the mirror unchanged but
    re-read, every candidate survives with its validation."""

    def incoming_taken(doc: dict[str, Any]) -> None:
        doc["decks"]["2"] = {"stable_id": "trk-other", "playing": True}

    with pytest.raises(DjError) as invalidated:
        _propose_with_mirror_changed(incoming_taken, requests)
    assert invalidated.value.code == "proposals_invalidated"
    kept = _propose_with_mirror_changed(lambda doc: None, requests)
    assert kept["proposals"] and kept["dropped"] == []
    assert all(row["validation"]["ok"] for row in kept["proposals"])


def test_propose_backfills_from_lower_ranked_candidates_after_the_recheck(requests: list[str]) -> None:
    """[if] the recheck drops the top-ranked candidates [then] lower-ranked ones that still validate fill the top N,
    [else stop]. Sol P1 #7595: the recheck saw only the rows already cut to the top. Control: the deck standing still
    returns the original top with nothing dropped."""
    from apps.autodj.plan import first_dispatch_bar, parse_plan

    still = _service(FakePage(out_bar=WINDOWS_AHEAD), requests).propose("trk-in", intent="smooth")
    first = min(
        row["plan"]["out"]["window"]["start_bar"] + first_dispatch_bar(parse_plan(row["plan"]))
        for row in still["proposals"]
    )
    moved = _propose_with_deck_moved_to(
        first - 0.4, requests, WINDOWS_AHEAD
    )  # every original top row is now inside the margin
    assert moved["dropped"] and len(moved["proposals"]) == len(still["proposals"])
    for row in moved["proposals"]:
        assert (
            row["plan"]["out"]["window"]["start_bar"] + first_dispatch_bar(parse_plan(row["plan"])) >= first - 0.4 + 1.0
        )
    assert still["dropped"] == []


def test_propose_holds_the_anchor_lead_on_the_final_reread(requests: list[str]) -> None:
    """[if] the deck advances during the search so a candidate's first command is between the 0.5 bar validation margin
    and the 1 bar anchor lead ahead [then] the final reread drops it with an anchor_lead reason, [else stop]. Sol P1
    #7595. Control: the same plan with more than the lead to spare is kept (the near case in the playhead test)."""
    from apps.autodj.plan import first_dispatch_bar, parse_plan

    still = _service(FakePage(out_bar=WINDOWS_AHEAD), requests).propose("trk-in", intent="smooth")
    first = min(
        row["plan"]["out"]["window"]["start_bar"] + first_dispatch_bar(parse_plan(row["plan"]))
        for row in still["proposals"]
    )
    moved = _propose_with_deck_moved_to(
        first - 0.7, requests, WINDOWS_AHEAD
    )  # 0.7 bar ahead: inside the lead, outside the margin
    assert any("anchor_lead" in row["reason"] for row in moved["dropped"]), moved["dropped"]
    for row in moved["proposals"]:
        assert (
            row["plan"]["out"]["window"]["start_bar"] + first_dispatch_bar(parse_plan(row["plan"])) >= first - 0.7 + 1.0
        )


def test_propose_rechecks_the_mirror_even_when_the_first_read_had_no_playhead(requests: list[str]) -> None:
    """[if] the first mirror read carries no outgoing playhead [then] the final recheck still runs and refuses a
    malformed or changed mirror, [else stop]. Sol P1 #7595. Control: an unchanged mirror returns candidates."""

    def without_playhead(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        page = FakePage(out_bar=100.0)
        page.mirror_doc["decks"]["1"].pop("position")
        first_read = page.mirror

        def changing() -> dict[str, Any]:
            doc = first_read()
            if page.mirror_reads >= 2:
                doc = copy.deepcopy(doc)
                change(doc)
            return doc

        page.mirror = changing  # type: ignore[method-assign]
        return _service(page, requests).propose("trk-in", intent="smooth")

    with pytest.raises(DjError) as malformed:
        without_playhead(lambda doc: doc.__setitem__("decks", None))
    assert malformed.value.code == "mirror_malformed"
    with pytest.raises(DjError) as invalidated:
        without_playhead(lambda doc: doc["decks"].__setitem__("2", {"stable_id": "trk-other", "playing": True}))
    assert invalidated.value.code == "proposals_invalidated"
    kept = without_playhead(lambda doc: None)
    assert kept["proposals"] and "out_bar_now" not in kept
