/**
 * WAVEPLAN-7896..7896d: a scheduled plan's automation as geometry on each deck's own track time.
 *
 * [if] a clock bar does not land on that deck's grid [then ⛔] the curve sits off the audio.
 * [if] the incoming deck moves after planning and the geometry stays put [then ⛔] the overlay lies.
 * [if] a ramp cut short by a later move keeps its full course [then ⛔] the curve disagrees with the executor.
 * [if] a deck has no grid or no tempo and curves are drawn anyway [then ⛔] the overlay invents a position.
 * [if] a move off the planned cue is not reported [then ⛔] nobody knows to re-plan.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const overlay = await loadTypeScriptModule('src/lib/rb/plan-overlay.ts');
const bars = await loadTypeScriptModule('src/lib/rb/plan-bars.ts');

const BAR_MS = 2000; // 120 BPM
const IN_BAR_MS = 1600; // 150 BPM on the incoming track's own grid
const clockGrid = Array.from({ length: 80 }, (_, bar) => bar * BAR_MS);
const inGrid = Array.from({ length: 80 }, (_, bar) => 300 + bar * IN_BAR_MS);

const ORDER = {
	clock: { deck: 1, stable_id: 'out' },
	incoming: { deck: 2, stable_id: 'in' },
	start_bar: 41,
	bars: 16,
	prepare: [
		{ type: 'seek', deck: 2, position_ms: 300 + 8 * IN_BAR_MS, exact: true },
		{ type: 'fader', deck: 2, value: 0 },
		{ type: 'eq', deck: 2, band: 'low', value: 0 }
	],
	events: [
		{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
		{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2 }, to: 1, over_bars: 8 } },
		{ at_bar: 4, ramp: { command: { type: 'filter', deck: 1 }, to: 0.2, over_bars: 8 } },
		{ at_bar: 8, command: { type: 'eq', deck: 1, band: 'low', value: 0 } },
		{ at_bar: 8, command: { type: 'eq', deck: 2, band: 'low', value: 0.5 } },
		{ at_bar: 8, ramp: { command: { type: 'fader', deck: 1 }, to: 0, over_bars: 8 } },
		{ at_bar: 16, command: { type: 'play', deck: 1, playing: false } }
	]
};

const CHANNELS = {
	1: { fader: 1, eq_low: 0.5, eq_mid: 0.5, eq_high: 0.5, filter: 0.5 },
	2: { fader: 0.8, eq_low: 0.5, eq_mid: 0.5, eq_high: 0.5, filter: 0.5 }
};

function input({
	clockMs = 70_000,
	inMs = 300 + 8 * IN_BAR_MS,
	inPlaying = false,
	ratio = 1,
	grid = inGrid,
	order = ORDER
} = {}) {
	return {
		order,
		channels: CHANNELS,
		liveKnobs: false,
		clock: { stable_id: 'out', position_ms: clockMs, downbeats: clockGrid },
		incoming: { stable_id: 'in', position_ms: inMs, downbeats: grid, playing: inPlaying },
		gridBarsPerClockBar: ratio
	};
}

const deckOf = (geometry, deck) => geometry.decks.find((row) => row.deck === deck);
const curveOf = (geometry, deck, name) =>
	deckOf(geometry, deck).curves.find((curve) => curve.name === name);

test('clock curves land on the clock grid: plan bar b is start_bar + b', () => {
	const geometry = overlay.buildPlanOverlay(input());
	const clock = deckOf(geometry, 1);
	assert.equal(clock.start_ms, 40 * BAR_MS);
	assert.equal(clock.end_ms, 56 * BAR_MS);
	assert.deepEqual(curveOf(geometry, 1, 'fader').points, [
		[40 * BAR_MS, 1],
		[48 * BAR_MS, 1],
		[56 * BAR_MS, 0]
	]);
});

test('a step draws as a vertical at its bar, on that deck', () => {
	const low = curveOf(overlay.buildPlanOverlay(input()), 1, 'eq_low').points;
	assert.deepEqual(low, [
		[40 * BAR_MS, 0.5],
		[48 * BAR_MS, 0.5],
		[48 * BAR_MS, 0],
		[56 * BAR_MS, 0]
	]);
});

test('incoming curves land on the incoming grid and start at the launch, from the prepared values', () => {
	const geometry = overlay.buildPlanOverlay(input());
	const incoming = deckOf(geometry, 2);
	const at = (bars) => 300 + bars * IN_BAR_MS;
	assert.equal(incoming.start_ms, at(8));
	assert.equal(incoming.end_ms, at(24));
	assert.deepEqual(curveOf(geometry, 2, 'fader').points, [
		[at(8), 0],
		[at(16), 1],
		[at(24), 1]
	]);
	assert.deepEqual(curveOf(geometry, 2, 'eq_low').points, [
		[at(8), 0],
		[at(16), 0],
		[at(16), 0.5],
		[at(24), 0.5]
	]);
});

test('a play dispatched exactly on a bar starts on the next downbeat, never on that bar', () => {
	const events = ORDER.events.map((event, index) =>
		index === 0 ? { ...event, at_bar: -1 } : event
	);
	const onTheBar = overlay.buildPlanOverlay({ ...input(), order: { ...ORDER, events } });
	assert.equal(deckOf(onTheBar, 2).start_ms, deckOf(overlay.buildPlanOverlay(input()), 2).start_ms);
	const later = ORDER.events.map((event, index) => (index === 0 ? { ...event, at_bar: 2 } : event));
	const fromBarThree = overlay.buildPlanOverlay({ ...input(), order: { ...ORDER, events: later } });
	// It launches at plan bar 3 from where the deck stands, so 13 bars are left of the plan's 16.
	assert.equal(deckOf(fromBarThree, 2).start_ms, 300 + 8 * IN_BAR_MS);
	assert.equal(deckOf(fromBarThree, 2).end_ms, 300 + 21 * IN_BAR_MS);
});

test('only the knobs the plan carries are drawn, plus fader and low EQ always', () => {
	const names = (deck) =>
		deckOf(overlay.buildPlanOverlay(input()), deck).curves.map((curve) => curve.name);
	assert.deepEqual(names(1), ['fader', 'eq_low', 'filter']);
	assert.deepEqual(names(2), ['fader', 'eq_low']);
});

test('STALE: cueing the idle incoming deck later moves every incoming point by the same bars, and the clock none', () => {
	const planned = overlay.buildPlanOverlay(input());
	const moved = overlay.buildPlanOverlay(input({ inMs: 300 + 12 * IN_BAR_MS }));
	const shift = 4 * IN_BAR_MS;
	assert.deepEqual(
		curveOf(moved, 2, 'fader').points,
		curveOf(planned, 2, 'fader').points.map(([ms, value]) => [ms + shift, value])
	);
	assert.deepEqual(curveOf(moved, 1, 'fader').points, curveOf(planned, 1, 'fader').points);
});

test('STALE: a beat jump on the PLAYING incoming deck re-derives against the clock deck, not the launch', () => {
	// Clock at plan bar 2, incoming two bars past its cue: in step with the plan, so nothing moved.
	const inStep = overlay.buildPlanOverlay(
		input({ clockMs: 42 * BAR_MS, inMs: 300 + 10 * IN_BAR_MS, inPlaying: true })
	);
	assert.equal(deckOf(inStep, 2).start_ms, 300 + 8 * IN_BAR_MS);
	assert.equal(inStep.anchor_moved, null);
	const jumped = overlay.buildPlanOverlay(
		input({ clockMs: 42 * BAR_MS, inMs: 300 + 11 * IN_BAR_MS, inPlaying: true })
	);
	assert.equal(deckOf(jumped, 2).start_ms, 300 + 9 * IN_BAR_MS);
});

test('the opposite way: the clock deck playing on alone never moves a playing incoming deck in step with it', () => {
	const early = overlay.buildPlanOverlay(
		input({ clockMs: 42 * BAR_MS, inMs: 300 + 10 * IN_BAR_MS, inPlaying: true })
	);
	const later = overlay.buildPlanOverlay(
		input({ clockMs: 45 * BAR_MS, inMs: 300 + 13 * IN_BAR_MS, inPlaying: true })
	);
	assert.deepEqual(deckOf(later, 2), deckOf(early, 2));
});

test('Beat Sync folding the tempo stretches the incoming span by the live ratio', () => {
	const geometry = overlay.buildPlanOverlay(input({ ratio: 2 }));
	const incoming = deckOf(geometry, 2);
	assert.equal(incoming.end_ms - incoming.start_ms, 32 * IN_BAR_MS);
});

test('a ramp cut short by a later move stops where the executor stops it', () => {
	const order = {
		...ORDER,
		events: [
			{ at_bar: 0, ramp: { command: { type: 'fader', deck: 1 }, to: 0, over_bars: 8 } },
			{ at_bar: 4, ramp: { command: { type: 'fader', deck: 1 }, to: 1, over_bars: 4 } }
		]
	};
	const curve = curveOf(overlay.buildPlanOverlay({ ...input(), order }), 1, 'fader').points;
	assert.deepEqual(curve, [
		[40 * BAR_MS, 1],
		[44 * BAR_MS, 0.5],
		[48 * BAR_MS, 1],
		[56 * BAR_MS, 1]
	]);
});

test('a move off the planned cue is reported in bars, and a move back clears it', () => {
	const moved = overlay.buildPlanOverlay(input({ inMs: 300 + 10.5 * IN_BAR_MS }));
	assert.deepEqual(moved.anchor_moved, { planned_bar: 9, live_bar: 11.5, moved_bars: 2.5 });
	assert.equal(overlay.buildPlanOverlay(input()).anchor_moved, null);
});

test('a deck with no grid or no tempo reports why and draws nothing, and the other deck still draws', () => {
	const noGrid = input();
	noGrid.incoming = { ...noGrid.incoming, downbeats: [] };
	const geometry = overlay.buildPlanOverlay(noGrid);
	assert.deepEqual(deckOf(geometry, 2).curves, []);
	assert.equal(deckOf(geometry, 2).unavailable, 'no_grid');
	assert.equal(deckOf(geometry, 1).unavailable, null);
	assert.equal(curveOf(geometry, 1, 'fader').points.length, 3);
	const noTempo = overlay.buildPlanOverlay(input({ ratio: null }));
	assert.equal(deckOf(noTempo, 2).unavailable, 'no_tempo');
});

test('a track swapped off the incoming deck stops drawing there', () => {
	const swapped = input();
	swapped.incoming = { ...swapped.incoming, stable_id: 'other' };
	assert.equal(deckOf(overlay.buildPlanOverlay(swapped), 2).unavailable, 'track_changed');
});

test('alignmentChanged ignores sample jitter and catches a real move, a tempo change and a new grid', () => {
	const base = overlay.incomingAlignment(input());
	assert.equal(overlay.alignmentChanged(base, base), false);
	const jitter = overlay.incomingAlignment(input({ inMs: 300 + 8 * IN_BAR_MS + 5 }));
	assert.equal(overlay.alignmentChanged(base, jitter), false);
	const nudged = overlay.incomingAlignment(input({ inMs: 300 + 8 * IN_BAR_MS + 40 }));
	assert.equal(overlay.alignmentChanged(base, nudged), true);
	assert.equal(
		overlay.alignmentChanged(base, overlay.incomingAlignment(input({ ratio: 1.01 }))),
		true
	);
	// A small ratio change that moves the span's far end by more than the tolerance must rebuild: 0.002 x 16 x 1600 ms.
	assert.equal(
		overlay.alignmentChanged(base, overlay.incomingAlignment(input({ ratio: 1.002 }))),
		true
	);
	assert.equal(
		overlay.alignmentChanged(base, overlay.incomingAlignment(input({ ratio: 1.0002 }))),
		false
	);
	assert.equal(overlay.alignmentChanged(base, null), true);
	assert.equal(overlay.alignmentChanged(null, null), false);
});

// The incoming track slows from 1,600 ms bars to 800 ms bars for its last ten, so its final interval is not local.
const slowingGrid = Array.from(
	{ length: 80 },
	(_, bar) => 300 + Math.min(bar, 70) * IN_BAR_MS + Math.max(0, bar - 70) * 800
);

test('alignmentChanged measures a move in track milliseconds where the curves are, not by the last bar', () => {
	const base = overlay.incomingAlignment(input({ grid: slowingGrid }));
	const nudged = overlay.incomingAlignment(
		input({ grid: slowingGrid, inMs: 300 + 8 * IN_BAR_MS + 30 })
	);
	assert.equal(
		overlay.alignmentChanged(base, nudged),
		true,
		'a 30 ms move on a 1,600 ms bar is a real move'
	);
	const jitter = overlay.incomingAlignment(
		input({ grid: slowingGrid, inMs: 300 + 8 * IN_BAR_MS + 5 })
	);
	assert.equal(overlay.alignmentChanged(base, jitter), false);
});

test('barAt before the grid uses the first interval, so it inverts msAtBar there', () => {
	const grid = [1000, 3000, 4000];
	const roundTrip = (ms) => Math.round(bars.msAtBar(grid, bars.barAt(grid, ms)) * 1000) / 1000;
	for (const ms of [0, 500, 999, 6000]) assert.equal(roundTrip(ms), ms);
});

test('the planned cue is the last prepared seek, the one the deck ends on', () => {
	const order = {
		...ORDER,
		prepare: [{ type: 'seek', deck: 2, position_ms: 300, exact: true }, ...ORDER.prepare]
	};
	assert.equal(overlay.buildPlanOverlay(input({ order })).anchor_moved, null);
});

test('a knob moved after preparation starts the curve from the live value, not the prepared one', () => {
	const prepared = overlay.buildPlanOverlay(input());
	assert.equal(curveOf(prepared, 2, 'fader').points[0][1], 0);
	const live = overlay.buildPlanOverlay({ ...input(), liveKnobs: true });
	assert.equal(curveOf(live, 2, 'fader').points[0][1], 0.8);
});

test('a build that fails still shows a pending plan on both decks, with no curves', () => {
	const failed = overlay.failedPlanOverlay(ORDER);
	assert.equal(failed.bars, 16);
	assert.equal(failed.anchor_moved, null);
	assert.deepEqual(
		failed.decks.map((row) => [
			row.deck,
			row.role,
			row.unavailable,
			row.curves.length,
			row.start_ms
		]),
		[
			[1, 'clock', 'build_failed', 0, null],
			[2, 'incoming', 'build_failed', 0, null]
		]
	);
});
