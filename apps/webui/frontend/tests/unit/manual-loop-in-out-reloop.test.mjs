/**
 * DECKUX-10478: manual LOOP IN, LOOP OUT, RELOOP/EXIT.
 *
 * Requirements:
 *   ✔︎ ✅ 🎯 LOOP IN stores a Q-snapped pending point.
 *     [if] Q is off [then] the raw playhead is used
 *   ✔︎ ✅ 🎯 LOOP OUT refuses without a pending in or when out is not after in.
 *     [if] pending and playhead are valid [then] engage pending..playhead
 *   ✔︎ ✅ 🎯 RELOOP exits an engaged loop or re-engages the last loop.
 *     [if] neither applies [then] refuse with a reason
 *   ✔︎ ✅ 🎯 Manual loop commands reach the engine via the typed dispatcher.
 *     [if] the deck is empty [then ⛔️] before transport runs
 *   ✔︎ ✅ 🎯 manual-loop-commands drives the port for IN, OUT and RELOOP.
 *     [if] setLoop rejects [then] pending IN is kept
 *   ✔︎ ✅ 🎯 clearLoadedTrackFields clears manual-loop memory on load reset.
 *     [if] has_rb_mapping was true [then] it is untouched
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const DRIFTING_GRID = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 },
	{ n: 1, bpm: 126, t: 2.04 }
];

let loops;
let actions;
let ipc;
let state;
let manualLoop;
let loadedFields;

before(async () => {
	loops = await loadTypeScriptModule('src/lib/player/transport/loops.ts');
	actions = await loadTypeScriptModule('src/lib/components/rb/deck/loop-cluster-actions.ts');
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	state = await loadTypeScriptModule('src/lib/player/state.svelte.ts');
	manualLoop = await loadTypeScriptModule('src/lib/rb/manual-loop-commands.ts');
	loadedFields = await loadTypeScriptModule('src/lib/rb/loaded-track-fields.ts');
});

function fakePort(st, playheadMs, handlers = {}) {
	const calls = [];
	return {
		calls,
		port: {
			deckState: () => st,
			playheadMs: () => playheadMs,
			setLoop: handlers.setLoop ?? (async (...a) => calls.push(['setLoop', ...a])),
			jumpIntoLoop: handlers.jumpIntoLoop ?? (async (...a) => calls.push(['jump', ...a]))
		}
	};
}

const TRUSTED_GRID = Array.from({ length: 32 }, (_, i) => ({
	t: i * 0.48,
	n: (i % 4) + 1,
	bpm: 125
}));

function loadedDeck(extra = {}) {
	const base = state._emptyDeckState(1);
	return {
		...base,
		stable_id: 't1',
		duration_ms: 60_000,
		quantize_enabled: true,
		quantize_grid_beats: 1,
		anlz: {
			beatgrid: {
				source: 'rekordbox',
				status: 'ok',
				beats: TRUSTED_GRID,
				beat_count: TRUSTED_GRID.length
			}
		},
		...extra
	};
}

test('manualLoopInMs leaves the playhead alone when Q is off', () => {
	assert.equal(loops.manualLoopInMs(null, 1500, 1), 1500);
});

test('manualLoopInMs snaps to the grid when Q is on', () => {
	assert.equal(loops.manualLoopInMs(DRIFTING_GRID, 700, 1), 608);
	assert.equal(loops.manualLoopInMs(DRIFTING_GRID, 1500, 4), 2040);
});

test('manualLoopOutDecision refuses without a pending in point', () => {
	const d = loops.manualLoopOutDecision(1, null, 2000);
	assert.equal(d.kind, 'refused');
	assert.match(d.reason, /press LOOP IN first/);
});

test('manualLoopOutDecision refuses when out is not after in', () => {
	for (const playhead of [1000, 500]) {
		const d = loops.manualLoopOutDecision(2, 1000, playhead);
		assert.equal(d.kind, 'refused');
		assert.match(d.reason, /must be after loop in/);
	}
});

test('manualLoopOutDecision engages a valid pending..playhead range', () => {
	const d = loops.manualLoopOutDecision(3, 1000, 3000);
	assert.deepEqual(d, { kind: 'engage', loop: { in_ms: 1000, out_ms: 3000 } });
});

test('reloopDecision exits an engaged loop', () => {
	const d = loops.reloopDecision(
		1,
		{ in_ms: 0, out_ms: 1000, engaged: true, beat_length: 4 },
		null
	);
	assert.deepEqual(d, { kind: 'exit' });
});

test('reloopDecision re-engages the last loop when nothing is engaged', () => {
	const last = { in_ms: 100, out_ms: 900, beat_length: null };
	const d = loops.reloopDecision(
		2,
		{ in_ms: 0, out_ms: 1000, engaged: false, beat_length: null },
		last
	);
	assert.deepEqual(d, { kind: 'reengage', loop: last });
});

test('reloopDecision refuses when there is no engaged loop and no last loop', () => {
	const d = loops.reloopDecision(4, null, null);
	assert.equal(d.kind, 'refused');
	assert.match(d.reason, /no previous loop/);
});

test('rememberedLoop records engaged loops and ignores non-engaged state', () => {
	const prev = { in_ms: 1, out_ms: 2, beat_length: 4 };
	const engaged = { in_ms: 10, out_ms: 20, engaged: true, beat_length: 8 };
	assert.deepEqual(loops.rememberedLoop(engaged, prev), { in_ms: 10, out_ms: 20, beat_length: 8 });
	assert.deepEqual(loops.rememberedLoop(null, prev), prev);
	assert.deepEqual(
		loops.rememberedLoop({ in_ms: 5, out_ms: 6, engaged: false, beat_length: null }, prev),
		prev
	);
});

test('manualLoopButtons reflect load, pending in, engaged and last-loop states', () => {
	const empty = state._emptyDeckState(1);
	const unloaded = actions.manualLoopButtons({ deck: empty, pending: false });
	assert.equal(unloaded.canIn, false);
	assert.match(unloaded.inTip, /load a track first/);

	const loaded = { ...empty, stable_id: 't1', duration_ms: 60_000 };
	const withPending = {
		...loaded,
		pending_loop_in_ms: 1000
	};
	const pendingIn = actions.manualLoopButtons({ deck: withPending, pending: false });
	assert.equal(pendingIn.inLit, true);
	assert.equal(pendingIn.canOut, true);

	const engaged = {
		...loaded,
		loop: { in_ms: 0, out_ms: 4000, engaged: true, beat_length: 4 }
	};
	const exitBtn = actions.manualLoopButtons({ deck: engaged, pending: false });
	assert.equal(exitBtn.outLit, true);
	assert.equal(exitBtn.reloopLabel, 'EXIT');

	const exited = {
		...loaded,
		loop: null,
		last_loop: { in_ms: 0, out_ms: 4000, beat_length: 4 }
	};
	const reloopBtn = actions.manualLoopButtons({ deck: exited, pending: false });
	assert.equal(reloopBtn.reloopLabel, 'RELOOP');
	assert.equal(reloopBtn.canReloop, true);

	const blocked = actions.manualLoopButtons({ deck: loaded, pending: true });
	assert.equal(blocked.canIn, false);
	assert.match(blocked.inTip, /deck command pending/);
});

test('loop_out claims the deck and sync scopes like beat_jump', () => {
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'loop_out', deck: 2 }), [2, 'sync']);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'beat_jump', deck: 2, beats: 4 }),
		ipc.performanceCommandQueueScopes({ type: 'loop_out', deck: 2 })
	);
});

test('malformed manual loop commands are rejected before the engine runs', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const handle = globalThis.window.musicDjToolsPerformance;
		await assert.rejects(
			() => handle.dispatch({ type: 'loop_in', deck: 1, extra: 1 }),
			/unexpected fields/i
		);
		await assert.rejects(
			() => handle.dispatch({ type: 'loop_out', deck: 9 }),
			/deck must be one of/i
		);
		for (const type of ['loop_in', 'loop_out', 'reloop']) {
			await assert.rejects(() => handle.dispatch({ type, deck: 1 }), /no track loaded/i);
		}
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('_emptyDeckState clears manual loop memory fields', () => {
	const st = state._emptyDeckState(3);
	assert.equal(st.pending_loop_in_ms, null);
	assert.equal(st.last_loop, null);
});

test('manualLoopIn stores raw playhead when Q is off and moves on second press', () => {
	const st = loadedDeck({ quantize_enabled: false, anlz: null });
	const first = fakePort(st, 1500);
	assert.equal(manualLoop.manualLoopIn(first.port, 1), 1500);
	assert.equal(st.pending_loop_in_ms, 1500);
	const second = fakePort(st, 2200);
	assert.equal(manualLoop.manualLoopIn(second.port, 1), 2200);
});

test('manualLoopIn snaps to the grid when Q is on', () => {
	const st = loadedDeck();
	const { port } = fakePort(st, 700);
	assert.equal(manualLoop.manualLoopIn(port, 1), 480);
});

test('manualLoopOut refuses without pending in and makes no port calls', async () => {
	const st = loadedDeck();
	const { port, calls } = fakePort(st, 2000);
	await assert.rejects(() => manualLoop.manualLoopOut(port, 1), /press LOOP IN first/);
	assert.equal(calls.length, 0);
});

test('manualLoopOut engages pending..playhead and clears pending', async () => {
	const st = loadedDeck({ pending_loop_in_ms: 1000 });
	const { port, calls } = fakePort(st, 3000);
	await manualLoop.manualLoopOut(port, 1);
	assert.deepEqual(calls, [['setLoop', 1, { in_ms: 1000, out_ms: 3000 }, true]]);
	assert.equal(st.pending_loop_in_ms, null);
});

test('manualLoopOut keeps pending when setLoop rejects', async () => {
	const st = loadedDeck({ pending_loop_in_ms: 1000 });
	const { port } = fakePort(st, 3000, {
		setLoop: async () => {
			throw new Error('setLoop failed');
		}
	});
	await assert.rejects(() => manualLoop.manualLoopOut(port, 1), /setLoop failed/);
	assert.equal(st.pending_loop_in_ms, 1000);
});

test('manualReloop exits an engaged loop via setLoop(null)', async () => {
	const st = loadedDeck({
		loop: { in_ms: 0, out_ms: 4000, engaged: true, beat_length: 4 }
	});
	const { port, calls } = fakePort(st, 500);
	await manualLoop.manualReloop(port, 1);
	assert.deepEqual(calls, [['setLoop', 1, null]]);
});

test('manualReloop re-engages last_loop via jumpIntoLoop', async () => {
	const st = loadedDeck({
		loop: null,
		last_loop: { in_ms: 100, out_ms: 900, beat_length: 4 }
	});
	const { port, calls } = fakePort(st, 500);
	await manualLoop.manualReloop(port, 1);
	assert.equal(calls.length, 1);
	assert.equal(calls[0][0], 'jump');
	assert.deepEqual(calls[0][2], {
		in_ms: 100,
		out_ms: 900,
		beat_length: 4,
		engaged: true
	});
});

test('manualReloop refuses with no engaged loop and no last loop', async () => {
	const st = loadedDeck({ loop: null, last_loop: null });
	const { port, calls } = fakePort(st, 500);
	await assert.rejects(() => manualLoop.manualReloop(port, 1), /no previous loop/);
	assert.equal(calls.length, 0);
});

test('manual loop commands refuse empty deck before the port runs', async () => {
	const st = state._emptyDeckState(2);
	const { port, calls } = fakePort(st, 0);
	assert.throws(() => manualLoop.manualLoopIn(port, 2), /no track loaded on deck 2/);
	await assert.rejects(() => manualLoop.manualLoopOut(port, 2), /no track loaded on deck 2/);
	await assert.rejects(() => manualLoop.manualReloop(port, 2), /no track loaded on deck 2/);
	assert.equal(calls.length, 0);
});

test('clearLoadedTrackFields clears manual-loop memory but not has_rb_mapping', () => {
	const st = loadedDeck({
		pending_loop_in_ms: 500,
		last_loop: { in_ms: 0, out_ms: 1000, beat_length: 4 },
		loop: { in_ms: 0, out_ms: 1000, engaged: true, beat_length: 4 },
		hot_cues: [{ slot: 1, position_ms: 0 }],
		has_rb_mapping: false
	});
	loadedFields.clearLoadedTrackFields(st);
	assert.equal(st.stable_id, null);
	assert.equal(st.pending_loop_in_ms, null);
	assert.equal(st.last_loop, null);
	assert.equal(st.loop, null);
	assert.deepEqual(st.hot_cues, []);
	assert.equal(st.has_rb_mapping, false);
});
