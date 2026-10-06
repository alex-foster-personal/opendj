/**
 * PERFMODE-04 playing gate: shed background work while a deck plays, and never
 * drop it.
 *
 * Regression lines:
 * - [if] a trigger arriving while a deck plays runs the work anyway [then] a
 *   stranger's rating PATCH still stalls the main thread mid-mix - broken
 * - [if] a deferred run is not owed after playback stops [then] the pane is
 *   silently stale for the rest of the session, which is worse than the stall
 * - [if] many triggers in one episode each book their own run [then] the
 *   deferral has only postponed the pile-up instead of collapsing it
 * - [if] a user interaction does not release the owed run [then] scrolling the
 *   library shows rows the app already knows are wrong
 * - [if] an interaction release runs synchronously [then] the work is back on
 *   the gesture path, which is the thing the gate exists to avoid
 * - [if] the gate emits a ring row per trigger rather than per episode [then]
 *   one bulk edit flushes the 40-row perf log it writes into
 * - [if] a release bypasses the coalescer the gate owns [then] C5 is back: one
 *   library.changed frame costs two concurrent refetches racing the same panes
 * - [if] anyDeckPlaying stops reading BOTH playing and audible [then] a refetch
 *   can land in the window at the start or the end of a mix
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const GATE_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/playing-gate.ts', import.meta.url)),
	'utf8'
);

let gateModule;

before(async () => {
	gateModule = await loadTypeScriptModule('src/lib/rb/playing-gate.ts', {
		viteApiBase: 'https://playing-gate.example.test'
	});
});

/** A gate whose transport, clock, scheduler and ring the test owns outright. */
function makeGate({ playing = false, run } = {}) {
	const state = {
		playing,
		runs: 0,
		rows: [],
		scheduled: [],
		timers: [],
		clock: 1000
	};
	const gate = gateModule.createPlayingGate({
		kind: 'library-refresh-deferred',
		run:
			run ??
			(async () => {
				state.runs += 1;
			}),
		isPlaying: () => state.playing,
		schedule: (task) => state.scheduled.push(task),
		now: () => state.clock,
		record: (kind, stages, deck, labels) => state.rows.push({ kind, stages, deck, labels }),
		setTimer: (task, ms) => state.timers.push({ task, at: state.clock + ms })
	});
	return { gate, state };
}

/** Let every already-queued microtask and continuation settle. */
function flush() {
	return new Promise((resolve) => setImmediate(resolve));
}

// ------------------------------------------------------------- the idle path

test('a trigger with nothing playing runs immediately, exactly as before', () => {
	const { gate, state } = makeGate();

	gate.request();

	assert.equal(state.runs, 1);
	assert.equal(gate.pending, false, 'nothing is owed');
	assert.equal(state.rows.length, 0, 'and an ungated run writes no deferral row');
});

// --------------------------------------------------------- deferral + drain

test('a trigger while a deck plays defers instead of refetching mid-mix', () => {
	const { gate, state } = makeGate({ playing: true });

	gate.request();

	assert.equal(state.runs, 0, 'no main-thread work while audio is live');
	assert.equal(gate.pending, true, 'but the run is owed, not dropped');
});

test('the owed run drains the moment playback stops', () => {
	const { gate, state } = makeGate({ playing: true });
	gate.request();

	state.playing = false;
	gate.drain();

	assert.equal(state.runs, 1);
	assert.equal(gate.pending, false);
});

test('drain is a no-op while a deck is still playing', () => {
	const { gate, state } = makeGate({ playing: true });
	gate.request();

	gate.drain();

	assert.equal(state.runs, 0, 'the effect can fire for any deck change - only a stop drains');
	assert.equal(gate.pending, true);
});

test('drain with nothing owed never manufactures a refetch', () => {
	const { gate, state } = makeGate();

	gate.drain();
	gate.drain();

	assert.equal(state.runs, 0);
});

test('a whole burst of invalidations collapses into one owed run', async () => {
	const { gate, state } = makeGate({ playing: true });

	// A bulk edit publishes library.changed per write, and one gap-revealing
	// frame fires both bus subscriptions for a single event.
	for (let i = 0; i < 40; i++) gate.request();

	assert.equal(gate.pending, true);
	state.playing = false;
	gate.drain();
	await flush();

	assert.equal(state.runs, 1, '40 triggers, one refetch');
	assert.equal(state.rows[0].stages.coalesced, 40, 'and the row says how many were absorbed');
});

test('a trigger arriving after the stop but before the drain closes the episode once', () => {
	const { gate, state } = makeGate({ playing: true });
	gate.request();

	state.playing = false;
	gate.request();

	assert.equal(state.runs, 1);
	assert.equal(gate.pending, false, 'the flag must not survive into a second redundant run');
	gate.drain();
	assert.equal(state.runs, 1);
});

test('the gate re-arms after an episode rather than latching', async () => {
	const { gate, state } = makeGate({ playing: true });
	gate.request();
	state.playing = false;
	gate.drain();
	await flush();

	state.playing = true;
	gate.request();
	assert.equal(gate.pending, true, 'a second set deferring again');
	state.playing = false;
	gate.drain();
	await flush();

	assert.equal(state.runs, 2);
	assert.equal(state.rows.length, 2, 'one row per episode, not one for all time');
});

// -------------------------------------------------------- the interaction path

test('touching the library releases the owed run even while audio is live', () => {
	const { gate, state } = makeGate({ playing: true });
	gate.request();

	gate.flushOnInteraction();

	assert.equal(gate.pending, false);
	assert.equal(state.runs, 0, 'not on the gesture path');
	assert.equal(state.scheduled.length, 1, 'yielded to an idle slot instead');
	state.scheduled[0]();
	assert.equal(state.runs, 1, 'and it does actually run');
});

test('an interaction with nothing owed does no work at all', () => {
	const { gate, state } = makeGate({ playing: true });

	// Wired to scroll, so the common case has to be free.
	for (let i = 0; i < 100; i++) gate.flushOnInteraction();

	assert.equal(state.runs, 0);
	assert.equal(state.scheduled.length, 0);
	assert.equal(state.rows.length, 0);
});

// ------------------------------------------------------------- the scroll path

/** playing-gate.ts's SCROLL_SETTLE_MS (module-private). */
const SCROLL_SETTLE_MS = 250;

/** Run every timer due by the gate's clock, oldest first, including re-arms. */
function fireDueTimers(state) {
	for (;;) {
		const i = state.timers.findIndex((t) => t.at <= state.clock);
		if (i < 0) return;
		const [t] = state.timers.splice(i, 1);
		t.task();
	}
}

test('a scroll releases owed work only once the scroll settles', () => {
	const { gate, state } = makeGate({ playing: true });
	gate.request();

	gate.noteScroll();
	state.clock += 100;
	gate.noteScroll();
	state.clock += 200;
	fireDueTimers(state);
	assert.equal(gate.pending, true, 'still scrolling 200 ms after the last event: nothing released');
	assert.equal(state.scheduled.length, 0);

	state.clock += SCROLL_SETTLE_MS;
	fireDueTimers(state);
	assert.equal(gate.pending, false);
	assert.equal(state.scheduled.length, 1, 'released off the gesture path, as an interaction is');
	assert.equal(state.rows[0].labels.resumedBy, 'user-interaction');
});

test('a trigger mid-scroll waits for the scroll to settle even with nothing playing', async () => {
	const { gate, state } = makeGate({ playing: false });
	gate.noteScroll();
	gate.request();
	assert.equal(state.runs, 0, 'a refetch must not land between two scroll frames');
	assert.equal(gate.pending, true);

	state.clock += SCROLL_SETTLE_MS;
	fireDueTimers(state);
	state.scheduled.splice(0).forEach((task) => task());
	await flush();
	assert.equal(state.runs, 1, 'and it does run once the scroll stops');

	// Control: with no scroll, the idle path still runs at once.
	state.clock += 10_000;
	gate.request();
	assert.equal(state.runs, 2);
});

test('a scroll with nothing owed arms no timer', () => {
	const { gate, state } = makeGate({ playing: true });
	for (let i = 0; i < 100; i++) gate.noteScroll();
	assert.equal(state.timers.length, 0);
	assert.equal(state.rows.length, 0);
});

test('a playback stop mid-scroll still waits for the scroll to settle', () => {
	const { gate, state } = makeGate({ playing: true });
	gate.request();
	gate.noteScroll();
	state.playing = false;
	gate.drain();
	assert.equal(gate.pending, true);
	state.clock += SCROLL_SETTLE_MS;
	fireDueTimers(state);
	assert.equal(gate.pending, false);
});

// ----------------------------------------------------------------- the ring row

test('one deferral episode writes exactly one ring row, carrying both numbers', () => {
	const { gate, state } = makeGate({ playing: true });
	gate.request();
	gate.request();
	gate.request();
	assert.equal(state.rows.length, 0, 'nothing is written while the episode is open');

	state.clock = 48_500;
	state.playing = false;
	gate.drain();

	assert.equal(state.rows.length, 1);
	const row = state.rows[0];
	assert.equal(row.kind, 'library-refresh-deferred');
	assert.equal(row.deck, null);
	assert.equal(row.stages.deferredMs, 47_500, 'how long the set was shielded');
	assert.equal(row.stages.coalesced, 3);
	assert.equal(row.labels.resumedBy, 'playback-stopped');
});

test('an interaction release is labelled as such', () => {
	const { gate, state } = makeGate({ playing: true });
	gate.request();

	gate.flushOnInteraction();

	assert.equal(state.rows[0].labels.resumedBy, 'user-interaction');
});

test('every stage value is a number, so the ring stays a ms/count map', () => {
	const { gate, state } = makeGate({ playing: true });
	gate.request();
	state.playing = false;
	gate.drain();

	for (const value of Object.values(state.rows[0].stages)) {
		assert.equal(typeof value, 'number');
		assert.ok(Number.isFinite(value));
	}
});

// ----------------------------------------------------- the coalescer it owns

test('C5: a release can never put two full refetches in flight at once', async () => {
	const gates = [];
	const live = { started: 0, peak: 0, now: 0 };
	const controllable = async () => {
		live.started += 1;
		live.now += 1;
		live.peak = Math.max(live.peak, live.now);
		const gate = {};
		gate.promise = new Promise((resolve) => {
			gate.resolve = resolve;
		});
		gates.push(gate);
		try {
			await gate.promise;
		} finally {
			live.now -= 1;
		}
	};
	const { gate, state } = makeGate({ run: controllable });

	// The gap-revealing library.changed frame: both bus subscriptions, one
	// event, nothing playing so neither is deferred.
	gate.request();
	gate.request();
	assert.equal(live.started, 1, 'the second trigger joins the run in flight');

	gates[0].resolve();
	await flush();
	assert.equal(live.started, 2, 'and is answered by exactly one trailing run');
	assert.equal(live.peak, 1, 'never two full refetches at once');

	gates[1].resolve();
	await flush();
	assert.equal(live.started, 2, 'and no third run');
	assert.equal(state.rows.length, 0, 'an ungated run is not a deferral');
});

test('a deferred release is coalesced against a run already in flight', async () => {
	const gates = [];
	const live = { started: 0, now: 0, peak: 0 };
	const controllable = async () => {
		live.started += 1;
		live.now += 1;
		live.peak = Math.max(live.peak, live.now);
		const gate = {};
		gate.promise = new Promise((resolve) => {
			gate.resolve = resolve;
		});
		gates.push(gate);
		try {
			await gate.promise;
		} finally {
			live.now -= 1;
		}
	};
	const { gate, state } = makeGate({ run: controllable });

	// A refresh starts while idle, a deck starts playing under it, and an
	// invalidation lands: the deferral must not become a second concurrent read.
	gate.request();
	state.playing = true;
	gate.request();
	state.playing = false;
	gate.drain();

	assert.equal(live.peak, 1, 'the gate release still goes through the coalescer');
	gates[0].resolve();
	await flush();
	assert.equal(live.started, 2, 'the trailing pass carries the deferred invalidation');
	gates[1].resolve();
	await flush();
});

test('the DEFAULT recorder is the real perf ring, not just an injectable seam', async () => {
	// Every test above injects `record`, so a typo in the production default
	// would be invisible. This one wires the gate to nothing but its defaults
	// and reads the row back out of the real ring.
	const real = await loadRuneModule(
		[
			"export { createPlayingGate } from '$lib/rb/playing-gate';",
			"export { readPerfEvents } from '$lib/rb/perf-event-log';"
		].join('\n')
	);
	let playing = true;
	let runs = 0;
	const gate = real.createPlayingGate({
		kind: 'library-refresh-deferred',
		isPlaying: () => playing,
		run: async () => {
			runs += 1;
		}
	});

	gate.request();
	gate.request();
	playing = false;
	gate.drain();
	await flush();

	assert.equal(runs, 1);
	const row = real.readPerfEvents().find((e) => e.kind === 'library-refresh-deferred');
	assert.notEqual(row, undefined, 'the deferral must be diagnosable after the fact');
	assert.equal(row.deck, null);
	assert.equal(row.stages.coalesced, 2);
	assert.equal(typeof row.stages.deferredMs, 'number');
	assert.equal(row.labels.resumedBy, 'playback-stopped');
});

// ------------------------------------------------------- createBackgroundDemandShed

/** A shed whose transport, pressure, xrun counter and clock the test owns outright. */
function makeShed({ playing = true, elevated = true, xruns = 0, jobs = [] } = {}) {
	const state = { playing, elevated, xruns, notified: [] };
	const shed = gateModule.createBackgroundDemandShed({
		isPlaying: () => state.playing,
		pressureElevated: () => state.elevated,
		readXruns: () => state.xruns,
		notify: (suggestion) => state.notified.push(suggestion),
		jobs
	});
	return { shed, state };
}

test('createBackgroundDemandShed refuses to register a P0 job', () => {
	assert.throws(
		() =>
			gateModule.createBackgroundDemandShed({
				isPlaying: () => false,
				pressureElevated: () => false,
				readXruns: () => 0,
				jobs: [{ id: 'audio-callbacks', run: async () => {} }]
			}),
		/audio-callbacks cannot be shed/
	);
});

test('a job requested while playing and elevated is owed, not run', () => {
	let ran = 0;
	const { shed } = makeShed({
		playing: true,
		elevated: true,
		jobs: [{ id: 'library-poll-cadence', run: async () => { ran += 1; } }]
	});

	shed.request('library-poll-cadence');

	assert.equal(ran, 0, 'the work effect must be absent while the gate is closed');
	assert.equal(shed.pending, true, 'but owed, never dropped');
});

test('the owed job actually runs once pressure clears', async () => {
	let ran = 0;
	const { shed, state } = makeShed({
		playing: true,
		elevated: true,
		jobs: [{ id: 'library-poll-cadence', run: async () => { ran += 1; } }]
	});

	shed.request('library-poll-cadence');
	assert.equal(ran, 0);

	state.elevated = false;
	shed.sync();
	await flush();

	assert.equal(ran, 1, 'the work effect must be present once the gate reopens');
	assert.equal(shed.pending, false);
});

test('a job requested while idle runs its real work immediately', () => {
	let ran = 0;
	const { shed } = makeShed({
		playing: false,
		elevated: false,
		jobs: [{ id: 'library-poll-cadence', run: async () => { ran += 1; } }]
	});

	shed.request('library-poll-cadence');

	assert.equal(ran, 1);
});

test('sync() suggests toasts exactly once per elevated episode, only while a deck plays', () => {
	const { shed, state } = makeShed({ playing: true, elevated: false, jobs: [] });

	shed.sync();
	assert.equal(state.notified.length, 0, 'idle: no suggestion');

	state.elevated = true;
	shed.sync();
	assert.equal(state.notified.length, gateModule.SHED_TOAST_SUGGESTIONS.length);

	shed.sync();
	assert.equal(
		state.notified.length,
		gateModule.SHED_TOAST_SUGGESTIONS.length,
		'one episode, one suggestion round, not one per sync tick'
	);
});

// ------------------------------------------------------------- anyDeckPlaying

test('anyDeckPlaying reports false on a fresh engine with no deck loaded', () => {
	assert.equal(typeof gateModule.anyDeckPlaying, 'function');
	assert.equal(gateModule.anyDeckPlaying(), false);
});

test('anyDeckPlaying watches both the scheduled and the audible transport', () => {
	const body = GATE_SOURCE.slice(
		GATE_SOURCE.indexOf('export function anyDeckPlaying()'),
		GATE_SOURCE.indexOf('export type IdleScheduler')
	);
	assert.ok(body.length > 0, 'if anyDeckPlaying cannot be located then this guard asserts nothing');
	assert.match(body, /for \(const deck of DECK_IDS\)/, 'every deck, not just the master');
	assert.match(
		body,
		/state\.playing \|\| state\.audible/,
		'either flag alone leaves a window at the start or the end of a mix'
	);
});
