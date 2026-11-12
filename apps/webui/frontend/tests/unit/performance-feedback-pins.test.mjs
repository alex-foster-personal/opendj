/**
 * Issue #13106: consolidated packet-1 performance feedback pin regressions.
 *
 * - [if] a named feedback pin has shipped behavior [then] a regression test names the pin id
 * - [if] the pinned behavior is removed [then] that test fails
 * - [if] an unrelated performance control changes [then] these pin tests stay green
 */
import assert from 'node:assert/strict';
import { before, it, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const DRIFTING_GRID = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 },
	{ n: 1, bpm: 126, t: 2.04 },
	{ n: 2, bpm: 126, t: 2.53 },
	{ n: 3, bpm: 126, t: 3.03 },
	{ n: 4, bpm: 126, t: 3.54 },
	{ n: 1, bpm: 125, t: 4.06 }
];

const DRIFTING_GRID_ANLZ = {
	beatgrid: {
		beats: DRIFTING_GRID
	}
};

function assertMs(actual, expected) {
	assert.ok(Math.abs(actual - expected) < 1e-6, `expected ${expected}ms, got ${actual}ms`);
}

let pairing;
let pickDoubleClickDeck;
let applyDeckTrackDrop;
let trackDragRefusal;
let math;
let loops;

before(async () => {
	pairing = await loadTypeScriptModule('tests/unit/fixtures/pairing-snapshot-entry.ts', {
		viteApiBase: 'https://pairing.example.test'
	});
	({ pickDoubleClickDeck } = await loadTypeScriptModule('src/lib/rb/deck-slots.ts'));
	({ applyDeckTrackDrop } = await loadTypeScriptModule('src/lib/rb/deck-track-drop.ts'));
	({ trackDragRefusal } = await loadTypeScriptModule('src/lib/rb/track-drag-refusal.ts'));
	math = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
	loops = await loadTypeScriptModule('src/lib/player/transport/loops.ts');
});

function runDrop({ row, occupied = false, stableId = 'SID-1' }) {
	const dispatches = [];
	const toasts = [];
	return applyDeckTrackDrop({
		deckId: 1,
		occupied,
		stableId,
		row,
		dispatch: async (command) => {
			dispatches.push(command);
		},
		toast: (message, kind) => toasts.push({ message, kind })
	}).then(() => ({ dispatches, toasts }));
}

test('af7cc3d4d321 pairing snapshot stays frozen after live deck and EQ changes', async () => {
	globalThis.window = {};
	const uninstall = pairing.installPerformanceBrowserIpc();
	try {
		pairing.uiPrefs.beat_sync_max = false;
		for (const deckId of [1, 2]) {
			const deck = pairing.deckStates[deckId];
			deck.stable_id = `track-${deckId}`;
			deck.title = `Track ${deckId}`;
			deck.position_ms = deckId * 1000;
			deck.anlz = null;
			Object.assign(pairing.mixerState.channels[deckId], {
				eq_low: 0.2,
				eq_mid: 0.5,
				eq_high: 0.8
			});
		}
		await pairing.dispatchPerformanceCommand({ type: 'pairing_snapshot_open' });
		const frozenPosition = pairing.queryPerformanceState().pairing_snapshot.decks[0].position_ms;
		pairing.deckStates[1].position_ms = 99999;
		pairing.mixerState.channels[1].eq_low = 0.95;
		const afterLive = pairing.queryPerformanceState().pairing_snapshot.decks[0];
		assert.equal(afterLive.position_ms, frozenPosition);
		assert.equal(afterLive.eq_adjusts.length, 2);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('af7cc3d4d321 beat_sync_max captures beat-number timestamps at open', async () => {
	globalThis.window = {};
	const uninstall = pairing.installPerformanceBrowserIpc();
	try {
		pairing.uiPrefs.beat_sync_max = true;
		for (const deckId of [1, 2, 3, 4]) {
			const deck = pairing.deckStates[deckId];
			deck.stable_id = null;
			deck.title = null;
			deck.position_ms = 0;
			deck.anlz = null;
		}
		const deck = pairing.deckStates[1];
		deck.stable_id = 'grid-track';
		deck.title = 'Grid Track';
		deck.position_ms = 700;
		deck.anlz = DRIFTING_GRID_ANLZ;
		await pairing.dispatchPerformanceCommand({ type: 'pairing_snapshot_open' });
		const ts = pairing.queryPerformanceState().pairing_snapshot.decks[0].timestamp;
		assert.equal(ts.unit, 'beats');
		assert.equal(ts.value, 2);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

it('d2c156a503bb double-click never targets the live master deck', () => {
	const decks = {
		1: { stable_id: 'a', playing: true, is_master: true, fader: 0.8, loadSeq: 1 },
		2: { stable_id: null, playing: false, is_master: false, fader: 0.8, loadSeq: 2 },
		3: { stable_id: 'c', playing: true, is_master: false, fader: 0.8, loadSeq: 3 },
		4: { stable_id: 'd', playing: true, is_master: false, fader: 0.8, loadSeq: 4 }
	};
	const out = pickDoubleClickDeck({ shift: false, replace: false, decks });
	assert.notEqual(out.deck, 1, 'the picker took the master deck');
	assert.equal(out.deck, 2);
});

test('72be3e505510 loadable track loads onto a deck cleared by unload', async () => {
	const { dispatches, toasts } = await runDrop({
		row: { file_exists: true, is_streaming: false },
		occupied: true
	});
	assert.equal(toasts.length, 0);
	assert.deepEqual(dispatches, [
		{ type: 'unload', deck: 1 },
		{ type: 'load', deck: 1, stable_id: 'SID-1' }
	]);
});

test('72be3e505510 streaming refusal matches double-click wording', () => {
	const phrase = "Streaming track: Open DJ can't play streaming services";
	assert.equal(trackDragRefusal({ file_exists: true, is_streaming: true }), phrase);
	assert.equal(
		trackDragRefusal({
			file_exists: false,
			is_streaming: true,
			file_path: 'tidal:tracks:99560085'
		}),
		"Tidal streaming track: Open DJ can't play streaming services"
	);
});

test('334a50710ef0 beat jump shifts engaged loop by PQTZ beat count', () => {
	const loop = { in_ms: 608, out_ms: 2530 };
	const beatIndexOf = (ms) =>
		DRIFTING_GRID.findIndex((beat) => Math.abs(beat.t * 1000 - ms) < 1e-6);
	const originalBeatLength = beatIndexOf(loop.out_ms) - beatIndexOf(loop.in_ms);
	assert.equal(originalBeatLength, 4);

	const shifted = loops.shiftLiveBeatLoopRangeMs(DRIFTING_GRID, loop, 1, 5000);
	const shiftedBeatLength = beatIndexOf(shifted.out_ms) - beatIndexOf(shifted.in_ms);
	assert.equal(
		shiftedBeatLength,
		originalBeatLength,
		'beat jump must never change the loop length'
	);

	const rawTarget = math.beatJumpTargetMs(DRIFTING_GRID, 2040, 1);
	const targetMs = math.beatJumpTargetWithinDurationMs(DRIFTING_GRID, rawTarget, 5000);
	const landedMs = loops.targetWithinShiftedLiveLoopMs(DRIFTING_GRID, targetMs, shifted);
	assert.ok(
		landedMs >= shifted.in_ms && landedMs < shifted.out_ms,
		'the jump must land the playhead inside the shifted loop, never exit it'
	);
});

test('e09c63131832 two rapid beat jumps compound on projected anchor', () => {
	const first = math.beatJumpTargetMs(DRIFTING_GRID, 135, 4);
	const second = math.beatJumpTargetMs(DRIFTING_GRID, first, 4);
	assertMs(first, 2040);
	assertMs(second, 4060);
	assert.notEqual(
		second,
		math.beatJumpTargetMs(DRIFTING_GRID, 135, 4),
		'a second jump must not reuse the original anchor'
	);
});

test('b91c8ba9b84b autoplay arm failure surfaces in console and hunt report', async () => {
	const stallEntry = [
		"export { noteAutoPlaySilentIdle, clearAutoPlayStall } from '$lib/rb/autoplay-stall.svelte';",
		"export { readPerfEvents } from '$lib/rb/perf-event-log';"
	].join('\n');
	const stallMod = await loadRuneModule(stallEntry);
	const calls = { error: [] };
	const realError = console.error;
	console.error = (...args) => {
		calls.error.push(args.join(' '));
	};
	try {
		stallMod.clearAutoPlayStall();
		stallMod.noteAutoPlaySilentIdle({ source_stable_id: 'src-b91c8ba9b84b', blocked: [] });
	} finally {
		console.error = realError;
		stallMod.clearAutoPlayStall();
	}
	const message = calls.error.find((line) => line.startsWith('[autoplay]'));
	assert.ok(
		message,
		`expected production [autoplay] console.error, saw: ${calls.error.join(' | ')}`
	);
	assert.match(message, /no-deck-playing after idle timeout/);
	const perfRows = stallMod.readPerfEvents().filter((row) => row.kind === 'autoplay-stall');
	assert.ok(
		perfRows.length >= 1,
		'autoplay-stall perf row must carry the same diagnostic for engine.log'
	);
	assert.match(perfRows.at(-1).message, /\[autoplay\] arm failed/);
});
