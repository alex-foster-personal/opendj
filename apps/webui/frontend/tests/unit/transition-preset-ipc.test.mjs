/** OJ-13717: transition presets on the performance bus, the CLI verb's TypeScript half. */
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// The engine reads the page URL for ?extroute=; these tests run as a page with no query.
globalThis.location ??= { search: '' };

let bus;
let guards;
let ipc;
let perf;

before(async () => {
	bus = await loadTypeScriptModule('tests/unit/fixtures/transition-preset-bus-entry.ts', {
		viteApiBase: 'https://transition-preset-bus.example.test'
	});
	guards = await loadTypeScriptModule('src/lib/rb/performance-ipc-guards.ts');
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	perf = await loadTypeScriptModule('tests/unit/fixtures/pairing-snapshot-entry.ts', {
		viteApiBase: 'https://transition-preset.example.test'
	});
});

afterEach(() => {
	bus._resetTransitionPresetsForTests();
	delete globalThis.requestAnimationFrame;
});

const ALLOWED = /bass-swap-8, filter-out-16, fader-blend-16/;

//-----------------------------------------------------------------------------
// helpers
//-----------------------------------------------------------------------------

function grid(n, beatMs = 500) {
	return Array.from({ length: n }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, time_ms: i * beatMs }));
}

/** A real performance query with decks 1 and 2 loaded and playing, deck 1 the master. */
function twoDeckState() {
	const state = structuredClone(perf.queryPerformanceState());
	for (const id of [1, 2]) {
		Object.assign(state.decks[id], {
			stable_id: `fixture:${id}`,
			playing: true,
			is_master: id === 1,
			position_ms: 1250,
			beatgrid: grid(64)
		});
		Object.assign(state.mixer.channels[id], {
			eq_low: 0.4,
			eq_mid: 0.5,
			eq_high: 0.6,
			filter: 0.45,
			fader: 0.9
		});
	}
	return state;
}

/** Frames on demand: a test-owned requestAnimationFrame, the browser global the host's clock waits on. */
function manualFrames() {
	const pending = [];
	globalThis.requestAnimationFrame = (cb) => pending.push(cb);
	return async function frameAt(state, positionMs) {
		for (const id of [1, 2]) state.decks[id].position_ms = positionMs;
		for (const cb of pending.splice(0)) cb(0);
		await new Promise((resolve) => setImmediate(resolve));
	};
}

/** The hotkey path's host over the same query, one tick per manual step. */
function hotkeyHost(state) {
	let tick = null;
	const host = {
		writes: [],
		readDecks: () => bus.presetDecksFromState(state),
		write: (command) => host.writes.push(command),
		schedule(fn) {
			tick = fn;
			return () => {
				tick = null;
			};
		},
		frameAt(positionMs) {
			for (const id of [1, 2]) state.decks[id].position_ms = positionMs;
			const run = tick;
			tick = null;
			run?.();
		}
	};
	return host;
}

//-----------------------------------------------------------------------------
// tests
//-----------------------------------------------------------------------------

/** [if] the bus gets transition_preset with a known id [then] it writes the same mixer moves its hotkey writes, [else stop]. */
test('if transition_preset on the bus moves the mixer differently from its preset.run hotkey then broken', async () => {
	const master = bus.presetDecksFromState(twoDeckState()).decks.find((deck) => deck.id === 1);
	assert.deepEqual(
		{ ...master, beatgrid: master.beatgrid.length },
		{
			id: 1,
			loaded: true,
			playing: true,
			isMaster: true,
			position_ms: 1250,
			beatgrid: 64,
			controls: { low: 0.4, mid: 0.5, high: 0.6, filter: 0.45, fader: 0.9 }
		},
		'the runner reads the deck and its mixer channel off the query'
	);
	const steps = [1500, 3000, 4999, 5200, 6000];
	for (const preset of bus.TRANSITION_PRESET_IDS) {
		bus._resetTransitionPresetsForTests();
		const keyed = hotkeyHost(twoDeckState());
		bus.runCommand(`preset.run.${preset}`, { timeStamp: 0, transitionPresets: keyed });
		for (const ms of steps) keyed.frameAt(ms);
		bus._resetTransitionPresetsForTests();

		const state = twoDeckState();
		const sent = [];
		const frameAt = manualFrames();
		bus.runTransitionPresetFromBus(preset, {
			query: () => state,
			dispatch: async (c) => sent.push(c)
		});
		for (const ms of steps) await frameAt(state, ms);

		assert.ok(keyed.writes.length > 0, `${preset}: the hotkey path wrote nothing`);
		assert.deepEqual(sent, keyed.writes, `${preset}: bus and hotkey writes differ`);
	}
});

/** [if] the bus sends a running preset again [then] the run aborts and writes stop, as a second hotkey press does, [else stop]. */
test('if a second transition_preset does not abort the running one then broken', async () => {
	const state = twoDeckState();
	const sent = [];
	const frameAt = manualFrames();
	const deps = { query: () => state, dispatch: async (c) => sent.push(c) };
	bus.runTransitionPresetFromBus('fader-blend-16', deps);
	await frameAt(state, 3000);
	assert.deepEqual(bus.activeTransitionPresetIds(), ['fader-blend-16']);
	bus.runTransitionPresetFromBus('fader-blend-16', deps);
	assert.deepEqual(bus.activeTransitionPresetIds(), []);
	const written = sent.length;
	await frameAt(state, 5000);
	assert.equal(sent.length, written, 'no write after the abort');
});

/** [if] a mixer write the run sent is refused [then] the run stops instead of moving the other controls on, [else stop]. */
test('if a refused preset write leaves the preset running then broken', async () => {
	const state = twoDeckState();
	const frameAt = manualFrames();
	bus.runTransitionPresetFromBus('fader-blend-16', {
		query: () => state,
		dispatch: async () => {
			throw new Error('fader refused');
		}
	});
	await frameAt(state, 3000);
	assert.deepEqual(bus.activeTransitionPresetIds(), []);
});

/** [if] transition_preset arrives with an unknown id [then] parse and dispatch both throw naming the allowed ids, [else stop]. */
test('if an unknown transition preset id is accepted or the refusal hides the allowed ids then broken', async () => {
	assert.deepEqual(guards._parseCommand({ type: 'transition_preset', preset: 'bass-swap-8' }), {
		type: 'transition_preset',
		preset: 'bass-swap-8'
	});
	assert.throws(
		() => guards._parseCommand({ type: 'transition_preset', preset: 'echo-out-8' }),
		ALLOWED
	);
	assert.throws(() => guards._parseCommand({ type: 'transition_preset' }), ALLOWED);
	assert.throws(() =>
		guards._parseCommand({ type: 'transition_preset', preset: 'bass-swap-8', deck: 1 })
	);
	assert.equal(
		ipc.performanceCommandQueueScopes({ type: 'transition_preset', preset: 'bass-swap-8' }),
		null
	);

	globalThis.window = {};
	const uninstall = perf.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			perf.dispatchPerformanceCommand({ type: 'transition_preset', preset: 'nope' }),
			ALLOWED
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

/** [if] the real dispatcher gets a known preset with no decks playing [then] it refuses exactly as the hotkey does, [else stop]. */
test('if transition_preset skips the preset runner the hotkey uses then broken', async () => {
	const idle = structuredClone(perf.queryPerformanceState());
	assert.throws(
		() =>
			bus.runCommand('preset.run.bass-swap-8', {
				timeStamp: 0,
				transitionPresets: hotkeyHost(idle)
			}),
		/no playing master deck/
	);
	globalThis.window = {};
	const uninstall = perf.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			perf.dispatchPerformanceCommand({ type: 'transition_preset', preset: 'bass-swap-8' }),
			(error) => error.name === 'TransitionPresetError' && error.code === 'no_master_deck'
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

//-----------------------------------------------------------------------------
// OJ-14133: the query reports the running preset
//-----------------------------------------------------------------------------

const IDLE = { running: null, started_at_ms: null, progress: null };

/** Start a preset the way the bus `transition_preset` dispatch does, on manual frames. */
function startFromBus(preset) {
	const state = twoDeckState();
	const frameAt = manualFrames();
	const deps = { query: () => state, dispatch: async () => {} };
	bus.runTransitionPresetFromBus(preset, deps);
	return { state, frameAt, deps };
}

/** [if] a preset is started over the bus [then] the next performance query reports its id running, with start time and progress, [else stop]. */
test('if a preset started over the bus is missing from the next performance query then broken', async () => {
	const before = Date.now();
	const { state, frameAt } = startFromBus('fader-blend-16');
	const started = bus.queryPerformanceState().transition_preset;
	assert.equal(started.running, 'fader-blend-16');
	assert.ok(started.started_at_ms >= before && started.started_at_ms <= Date.now());
	assert.equal(started.progress, 0);
	await frameAt(state, 6000);
	const mid = bus.queryPerformanceState().transition_preset;
	assert.equal(mid.running, 'fader-blend-16');
	assert.ok(mid.progress > 0 && mid.progress < 1, `progress mid-run was ${mid.progress}`);
	assert.equal(mid.started_at_ms, started.started_at_ms);
});

/** [if] the preset finishes or is aborted [then] the next performance query reports running null, [else stop]. */
test('if a finished or aborted preset still shows as running in the performance query then broken', async () => {
	const finished = startFromBus('fader-blend-16');
	await finished.frameAt(finished.state, 3000);
	assert.equal(bus.queryPerformanceState().transition_preset.running, 'fader-blend-16');
	await finished.frameAt(finished.state, 60_000);
	assert.deepEqual(bus.activeTransitionPresetIds(), [], 'the run completed');
	assert.deepEqual(bus.queryPerformanceState().transition_preset, IDLE);

	const aborted = startFromBus('bass-swap-8');
	await aborted.frameAt(aborted.state, 3000);
	assert.equal(bus.queryPerformanceState().transition_preset.running, 'bass-swap-8');
	bus.runTransitionPresetFromBus('bass-swap-8', aborted.deps);
	assert.deepEqual(bus.queryPerformanceState().transition_preset, IDLE);
});

/** [if] no preset ever ran [then] the performance query still carries transition_preset with running null, [else stop]. */
test('if the performance query omits transition_preset before any preset ran then broken', () => {
	assert.deepEqual(perf.queryPerformanceState().transition_preset, IDLE);
});
