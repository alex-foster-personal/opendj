"""LIGHT-10565 show runtime acceptance tests (issue #9902).

[if] deck blend colour, phrase handover, ambient-on-stop, or the daemon's ShowLoop wiring breaks [then] fail, [else stop].
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.feature_flags import load_flags
from apps.lighting.beat_clock import DeckSample
from apps.lighting.contract import LIGHTING_FLAG_ID
from apps.lighting.http import bind_lighting_runtime, router
from apps.lighting.runtime import (
    QUEUE_SIZE,
    SLOW_CONSUMER_CODE,
    DeckFeed,
    FeedFrame,
    LightingRuntime,
    ReplayFeed,
    ShowLoop,
    _beat_at,
)
from apps.lighting.show_runtime import (
    DeckPlan,
    DeckState,
    HandoverMode,
    ShowCue,
    ShowFrame,
    ShowState,
    current_cue,
    step,
)

pytestmark = [pytest.mark.requirement("LIGHT-10565")]


def _plan(track: str, looks: tuple[str, ...], phrase_beats: tuple[int, ...] = (0, 32, 64)) -> DeckPlan:
    cues = tuple(ShowCue(at_beat=i * 32, look=look, effect_level=i % 3) for i, look in enumerate(looks))
    return DeckPlan(track_id=track, cues=cues, phrase_beats=phrase_beats)


PLAN_A = _plan("ta", ("a-red", "a-amber", "a-red"))
PLAN_B = _plan("tb", ("b-blue", "b-cyan", "b-blue"))

GRID = [{"n": i % 4 + 1, "time_ms": i * 500.0, "bpm": 120.0} for i in range(64)]
GRID_B = [{"n": (i + 1) % 4 + 1, "time_ms": i * 500.0, "bpm": 120.0} for i in range(64)]
HOST_NS = 1_000_000_000_000


def _sample(deck: str, position_ms: float, *, playing: bool = True) -> DeckSample:
    return DeckSample(
        deck=deck,
        position_ms=position_ms,
        rate=1.0,
        playing=playing,
        host_time_ns=HOST_NS + round(position_ms * 1_000_000),
    )


def _feed_frame(
    positions: dict[str, float],
    weights: dict[str, float],
    *,
    playing: bool = True,
    grids: dict[str, list[dict[str, float | int]]] | None = None,
    plans: dict[str, DeckPlan | None] | None = None,
) -> FeedFrame:
    grids = grids or {}
    plans = plans or {"A": PLAN_A, "B": PLAN_B}
    decks: list[DeckFeed] = []
    host_ns = HOST_NS
    for deck, pos in sorted(positions.items()):
        sample = _sample(deck, pos, playing=playing)
        host_ns = sample.host_time_ns
        grid = grids.get(deck, GRID if deck == "A" else GRID_B)
        plan = plans.get(deck, PLAN_A if deck == "A" else PLAN_B)
        decks.append(DeckFeed(sample=sample, grid=grid, plan=plan))
    return FeedFrame(host_time_ns=host_ns, decks=tuple(decks), weights=weights)


def _app_show(
    tmp_path: Path,
    runtime: LightingRuntime,
) -> FastAPI:
    flag_dir = tmp_path / "flags"
    flag_dir.mkdir()
    (flag_dir / "feature-flags.json").write_text(json.dumps({LIGHTING_FLAG_ID: True}))
    app = FastAPI()
    app.state.feature_flags = load_flags(flag_dir)
    bind_lighting_runtime(app, runtime)
    app.include_router(router, prefix="/api/v1")
    return app


def _drain_ws(ws: Any) -> tuple[list[dict[str, Any]], int | None]:
    messages: list[dict[str, Any]] = []
    code: int | None = None
    try:
        while True:
            messages.append(ws.receive_json())
    except WebSocketDisconnect as exc:
        code = exc.code
    return messages, code


def _deck(deck: str, beat: int, weight: float, plan: DeckPlan, playing: bool = True, offset: int = 0) -> DeckState:
    return DeckState(
        deck=deck, playing=playing, beat_index=beat, bar_beat=(beat + offset) % 4 + 1, weight=weight, plan=plan
    )


def _owner_changes(frames: list[tuple[int, ShowFrame]]) -> list[int]:
    return [frames[i][0] for i in range(1, len(frames)) if frames[i][1].colour_deck != frames[i - 1][1].colour_deck]


def test_blend_crossfade_hands_colour_to_b_on_a_downbeat_once() -> None:
    state = ShowState()
    frames: list[tuple[int, ShowFrame]] = []
    for beat in range(64):
        wb = beat / 64
        if 28 <= beat <= 36:
            wb = 0.5 + (0.06 if beat % 2 else -0.06)
        for _ in range(4):
            a = _deck("A", beat, 1 - wb, PLAN_A)
            b = _deck("B", beat, wb, PLAN_B, offset=1)
            state, frame = step(state, [a, b], HandoverMode.BLEND)
            frames.append((beat, frame))
            assert frame.position_deck == frame.colour_deck
            if frame.colour_deck == "B" and frames[-2][1].colour_deck == "A":
                assert b.bar_beat == 1 and wb > 0.5
    changes = _owner_changes(frames)
    assert frames[0][1].colour_deck == "A" and frames[-1][1].colour_deck == "B"
    assert all(later - earlier >= 4 for earlier, later in itertools.pairwise(changes)), changes


def test_blend_never_flips_within_four_beats_when_wobbling() -> None:
    state = ShowState()
    frames: list[tuple[int, ShowFrame]] = []
    for beat in range(64):
        wb = 0.5 + (0.1 if (beat // 2) % 2 else -0.1)
        a, b = _deck("A", beat, 1 - wb, PLAN_A), _deck("B", beat, wb, PLAN_B, offset=1)
        state, frame = step(state, [a, b], HandoverMode.BLEND)
        frames.append((beat, frame))
    changes = _owner_changes(frames)
    assert changes, "wobble should cause some handovers"
    assert all(later - earlier >= 4 for earlier, later in itertools.pairwise(changes)), changes


def test_blend_layer_intensity_equals_deck_weight() -> None:
    _, frame = step(ShowState(), [_deck("A", 40, 0.7, PLAN_A), _deck("B", 3, 0.3, PLAN_B)], HandoverMode.BLEND)
    assert [(lay.deck, lay.intensity, lay.look) for lay in frame.layers] == [
        ("A", 0.7, "a-amber"),
        ("B", 0.3, "b-blue"),
    ]


def test_master_only_follows_incoming_only_on_its_phrase_boundary() -> None:
    state, _ = step(ShowState(), [_deck("A", 100, 1.0, PLAN_A), _deck("B", 9, 0.0, PLAN_B)], HandoverMode.MASTER_ONLY)
    for b_beat in range(10, 41):
        decks = [_deck("A", 100 + b_beat, 0.0, PLAN_A), _deck("B", b_beat, 1.0, PLAN_B)]
        state, frame = step(state, decks, HandoverMode.MASTER_ONLY)
        assert frame.colour_deck == ("A" if b_beat < 32 else "B"), b_beat
        assert len(frame.layers) == 1 and frame.layers[0].intensity == 1.0
    skip_state = ShowState(owner="A", owner_since_beat=0, last_beat={"A": 131, "B": 31})
    _, frame = step(skip_state, [_deck("A", 133, 0.0, PLAN_A), _deck("B", 33, 1.0, PLAN_B)], HandoverMode.MASTER_ONLY)
    assert frame.colour_deck == "B"


BLENDING_MODES = [HandoverMode.BLEND, HandoverMode.UPFADER_ONLY]


@pytest.mark.parametrize("mode", BLENDING_MODES)
def test_zero_weight_deck_contributes_no_layer(mode: HandoverMode) -> None:
    """A playing deck at weight 0 is left out of the layers; the weighted deck keeps full intensity."""
    _, frame = step(ShowState(), [_deck("A", 8, 1.0, PLAN_A), _deck("B", 8, 0.0, PLAN_B)], mode)
    assert [(lay.deck, lay.intensity, lay.look) for lay in frame.layers] == [("A", 1.0, "a-red")]
    assert frame.colour_deck == "A" and frame.position_deck == "A" and not frame.ambient


@pytest.mark.parametrize("mode", BLENDING_MODES)
def test_zero_weight_deck_never_takes_colour(mode: HandoverMode) -> None:
    """A zero-weight deck crossing its downbeats never takes colour from the weighted owner."""
    state = ShowState()
    for beat in range(16):
        state, frame = step(state, [_deck("A", 40 + beat, 0.0, PLAN_A), _deck("B", beat, 1.0, PLAN_B)], mode)
        assert frame.colour_deck == "B", beat
        assert [lay.deck for lay in frame.layers] == ["B"]


@pytest.mark.parametrize("mode", BLENDING_MODES)
def test_owner_dropping_to_zero_weight_hands_over_on_next_downbeat(mode: HandoverMode) -> None:
    """When the owner falls to weight 0 its layer vanishes at once, but colour waits for B's downbeat."""
    state, _ = step(ShowState(), [_deck("A", 0, 1.0, PLAN_A), _deck("B", 0, 0.0, PLAN_B)], mode)
    for beat in range(1, 5):
        state, frame = step(state, [_deck("A", beat, 1.0, PLAN_A), _deck("B", beat, 0.0, PLAN_B)], mode)
    state, frame = step(state, [_deck("A", 5, 0.0, PLAN_A), _deck("B", 5, 1.0, PLAN_B)], mode)
    assert [lay.deck for lay in frame.layers] == ["B"] and frame.colour_deck == "A"
    for beat in (6, 7):
        state, frame = step(state, [_deck("A", beat, 0.0, PLAN_A), _deck("B", beat, 1.0, PLAN_B)], mode)
        assert frame.colour_deck == "A", beat
    _, frame = step(state, [_deck("A", 8, 0.0, PLAN_A), _deck("B", 8, 1.0, PLAN_B)], mode)
    assert frame.colour_deck == "B" and frame.position_deck == "B"


@pytest.mark.parametrize("mode", BLENDING_MODES)
def test_equal_weights_give_equal_layers_with_own_looks(mode: HandoverMode) -> None:
    """Two decks at equal weight each get a layer at that same intensity showing their own plan's look."""
    _, frame = step(ShowState(), [_deck("B", 33, 0.5, PLAN_B), _deck("A", 2, 0.5, PLAN_A)], mode)
    assert [(lay.deck, lay.intensity, lay.look) for lay in frame.layers] == [
        ("A", 0.5, "a-red"),
        ("B", 0.5, "b-cyan"),
    ]


@pytest.mark.parametrize("owner", ["A", "B"])
@pytest.mark.parametrize("mode", BLENDING_MODES)
def test_equal_weight_tie_keeps_current_owner(mode: HandoverMode, owner: str) -> None:
    """An equal-weight tie never moves colour off the owner, even on the other deck's downbeat after the hold."""
    state = ShowState(owner=owner, owner_since_beat=0, last_beat={"A": 15, "B": 15})
    for beat in range(16, 33):
        state, frame = step(state, [_deck("A", beat, 0.5, PLAN_A), _deck("B", beat, 0.5, PLAN_B)], mode)
        assert frame.colour_deck == owner and frame.position_deck == owner, beat
        assert [lay.intensity for lay in frame.layers] == [0.5, 0.5]


def test_master_only_gives_followed_deck_one_full_layer_regardless_of_weights() -> None:
    """master_only emits only the followed deck's layer at intensity 1.0, never the weight, never the other deck."""
    state = ShowState(owner="A", owner_since_beat=0, last_beat={"A": 9, "B": 9})
    _, frame = step(state, [_deck("A", 10, 0.4, PLAN_A), _deck("B", 10, 0.6, PLAN_B)], HandoverMode.MASTER_ONLY)
    assert [(lay.deck, lay.intensity, lay.look) for lay in frame.layers] == [("A", 1.0, "a-red")]
    assert frame.colour_deck == "A" and frame.position_deck == "A"


def test_master_only_stopped_followed_deck_hands_over_without_waiting_for_phrase() -> None:
    """If the followed deck stops, master_only follows the remaining deck at once, off any phrase boundary."""
    state = ShowState(owner="A", owner_since_beat=0, last_beat={"A": 50, "B": 10})
    decks = [_deck("A", 51, 1.0, PLAN_A, playing=False), _deck("B", 11, 1.0, PLAN_B)]
    new_state, frame = step(state, decks, HandoverMode.MASTER_ONLY)
    assert frame.colour_deck == "B" and new_state.owner == "B" and new_state.owner_since_beat == 11
    assert [(lay.deck, lay.intensity) for lay in frame.layers] == [("B", 1.0)]


def test_both_decks_stopped_hands_over_to_ambient() -> None:
    state, _ = step(ShowState(), [_deck("A", 5, 1.0, PLAN_A), _deck("B", 5, 0.0, PLAN_B)], HandoverMode.BLEND)
    stopped = [_deck("A", 6, 1.0, PLAN_A, playing=False), _deck("B", 6, 0.0, PLAN_B, playing=False)]
    state, frame = step(state, stopped, HandoverMode.BLEND)
    assert frame.ambient and frame.layers == () and frame.colour_deck is None and frame.position_deck is None
    assert state.owner is None
    state, frame = step(state, [_deck("A", 7, 1.0, PLAN_A), stopped[1]], HandoverMode.BLEND)
    assert not frame.ambient and frame.colour_deck == "A"


def test_current_cue_and_one_beat_hit() -> None:
    plan = DeckPlan("t", (ShowCue(0, "intro", 0), ShowCue(16, "drop", 2, hit=True)), (0,))
    assert current_cue(plan, 15).look == "intro" and current_cue(plan, 20).look == "drop"
    _, on_hit = step(ShowState(), [DeckState("A", True, 16, 1, 1.0, plan)], HandoverMode.BLEND)
    _, after = step(ShowState(), [DeckState("A", True, 17, 2, 1.0, plan)], HandoverMode.BLEND)
    assert on_hit.layers[0].hit and not after.layers[0].hit


@pytest.mark.parametrize(
    ("decks", "match"),
    [
        ([_deck("A", 0, 1.2, PLAN_A)], "deck A"),
        ([DeckState("B", True, 0, 1, 0.5, None)], "deck B"),
        ([_deck("A", 0, 0.5, PLAN_A), _deck("A", 0, 0.5, PLAN_B)], "deck A"),
    ],
)
def test_invalid_inputs_fail_fast(decks: list[DeckState], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        step(ShowState(), decks, HandoverMode.BLEND)


def test_show_loop_fans_frames_to_every_sink() -> None:
    out_a: list[ShowFrame] = []
    out_b: list[ShowFrame] = []
    loop = ShowLoop(HandoverMode.BLEND, [out_a.append])
    loop.add_sink(out_b.append)
    for beat in range(3):
        loop.tick([_deck("A", beat, 1.0, PLAN_A)])
    assert out_a == out_b and len(out_a) == 3
    assert loop.state.owner == "A"


def test_runtime_process_ticks_show_loop_and_fans_to_frame_sinks() -> None:
    """[if] process runs with a frame sink [then] ShowLoop ticks and fans each frame."""
    seen: list[ShowFrame] = []
    runtime = LightingRuntime(frame_sinks=[seen.append])
    for pos in (0.0, 500.0, 1000.0):
        runtime.process(_feed_frame({"A": pos}, {"A": 1.0}))
    assert len(seen) == 3
    assert all(not f.ambient and f.colour_deck == "A" for f in seen)
    assert seen[0].layers[0].look == "a-red"
    assert runtime.show.state.owner == "A"


def test_runtime_publishes_show_frames_to_events_hub_on_change() -> None:
    """[if] identical frames repeat [then] show_frame publishes only on change."""
    runtime = LightingRuntime()
    sub = runtime.hub.attach()
    frame = _feed_frame({"A": 0.0}, {"A": 1.0})
    runtime.process(frame)
    runtime.process(frame)
    stopped = _feed_frame({"A": 100.0, "B": 100.0}, {"A": 0.0, "B": 0.0}, playing=False)
    runtime.process(stopped)

    received: list[dict[str, Any]] = []
    while True:
        try:
            received.append(sub.queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    show_msgs = [m for m in received if m is not None and m.get("type") == "show_frame"]
    assert len(show_msgs) == 2
    assert show_msgs[0]["layers"][0]["deck"] == "A"
    assert show_msgs[0]["colour_deck"] == "A"
    assert show_msgs[0]["at_host_ns"] == frame.host_time_ns
    assert show_msgs[1]["ambient"] is True


def test_runtime_deck_without_plan_or_grid_does_not_crash_and_is_ambient() -> None:
    """[if] a deck has no plan or empty grid [then] process yields ambient without error."""
    runtime = LightingRuntime()
    frame = FeedFrame(
        host_time_ns=HOST_NS,
        decks=(
            DeckFeed(sample=_sample("A", 0.0), grid=GRID, plan=None),
            DeckFeed(sample=_sample("B", 0.0), grid=(), plan=PLAN_B),
        ),
        weights={"A": 0.5, "B": 0.5},
    )
    result = runtime.process(frame)
    assert result.ambient


def test_runtime_beat_from_grid_drives_downbeat_handover() -> None:
    """[if] runtime crossfades A to B [then] colour hands to B only on B's downbeat past 0.5 weight."""
    seen: list[ShowFrame] = []
    runtime = LightingRuntime(frame_sinks=[seen.append])
    for beat in range(64):
        wb = beat / 64
        for sub in range(4):
            pos_ms = beat * 500.0 + sub * 125.0
            runtime.process(
                _feed_frame({"A": pos_ms, "B": pos_ms}, {"A": 1 - wb, "B": wb})
            )
    assert seen[0].colour_deck == "A" and seen[-1].colour_deck == "B"
    flips = [
        i
        for i in range(1, len(seen))
        if seen[i].colour_deck != seen[i - 1].colour_deck
    ]
    assert len(flips) == 1
    flip = flips[0]
    assert seen[flip - 1].colour_deck == "A" and seen[flip].colour_deck == "B"
    assert (flip // 4) / 64 > 0.5
    beat_info = _beat_at(GRID_B, (flip // 4) * 500.0 + (flip % 4) * 125.0)
    assert beat_info is not None
    _, bar_beat = beat_info
    assert bar_beat == 1


def test_runtime_master_only_mode_is_passed_to_show_loop() -> None:
    """[if] master_only is selected [then] ShowLoop mode and single full layer apply."""
    runtime = LightingRuntime(show_mode=HandoverMode.MASTER_ONLY)
    assert runtime.show.mode is HandoverMode.MASTER_ONLY
    frame = runtime.process(_feed_frame({"A": 5000.0, "B": 5000.0}, {"A": 0.0, "B": 1.0}))
    assert len(frame.layers) == 1 and frame.layers[0].intensity == 1.0


@pytest.mark.timeout(20)
def test_events_socket_receives_show_frames_from_replayed_feed(tmp_path: Path) -> None:
    """[if] HTTP-bound runtime replays a feed [then] at least one show_frame arrives."""
    frames = [
        _feed_frame({"A": float(pos)}, {"A": 1.0})
        for pos in (0.0, 500.0, 1000.0, 1500.0)
    ]
    runtime = LightingRuntime()
    runtime.feed = ReplayFeed(frames, start_when=lambda: runtime.hub.wait_for_subscribers(1))
    app = _app_show(tmp_path, runtime)
    with TestClient(app) as client, client.websocket_connect("/api/v1/lighting/events") as ws:
        messages, code = _drain_ws(ws)
    show_msgs = [m for m in messages if m["type"] == "show_frame"]
    assert any(m.get("colour_deck") == "A" for m in show_msgs)
    assert code == 1000


@pytest.mark.requirement("LIGHT-13355")
def test_slow_subscriber_overflows_bounded_while_healthy_one_keeps_receiving() -> None:
    """[if] one subscriber stops consuming [then] it is capped and marked 1013 while a healthy one gets every message."""
    runtime = LightingRuntime()
    slow = runtime.hub.attach()
    healthy = runtime.hub.attach()
    received: list[dict[str, Any]] = []
    total = QUEUE_SIZE * 2
    for index in range(total):
        runtime.hub.publish({"type": "probe", "index": index})
        item = healthy.queue.get_nowait()
        assert item is not None
        received.append(item)

    assert slow.overflow.is_set()
    assert slow.close_code == SLOW_CONSUMER_CODE
    assert slow.close_reason == "slow consumer"
    assert slow.queue.qsize() == QUEUE_SIZE
    assert not healthy.overflow.is_set()
    assert [m["index"] for m in received] == list(range(total))
    assert [m["seq"] for m in received] == list(range(1, total + 1))
    assert runtime.hub.subscriber_count == 2
