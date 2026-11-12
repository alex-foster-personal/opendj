// [if] commanded moves run and MIDI writes the same control [then] MIDI cancels
// the move and dispatches the value; MIDI on other controls does not.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve, join, dirname } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

globalThis.location ??= { search: '' };

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const FE_ROOT = FRONTEND_ROOT;

let vite;
let glue;
let runner;
let knobs;
let audioEngine;
let performanceIpc;
let uninstallPerformanceIpc;

let t;
/** @type {Array<() => void>} */
let pending;

const BEAT_MS = 60000 / 124;

function linearPlan(from, to, startMs, endMs) {
	return {
		startMs,
		endMs,
		valueAt(tm) {
			const span = endMs - startMs;
			if (span <= 0) return to;
			const u = Math.max(0, Math.min(1, (tm - startMs) / span));
			return from + (to - from) * u;
		}
	};
}

before(async () => {
	const listeners = new Map();
	const store = new Map();
	globalThis.window = {
		addEventListener: (type, fn) => listeners.set(type, fn),
		removeEventListener: (type) => listeners.delete(type),
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	globalThis.HTMLElement = class FakeHTMLElement {
		constructor(tagName, { isContentEditable = false } = {}) {
			this.tagName = tagName;
			this.isContentEditable = isContentEditable;
		}
	};
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	glue = await vite.ssrLoadModule('/src/lib/rb/midi/action-glue.svelte.ts');
	runner = await vite.ssrLoadModule('/src/lib/rb/commanded-move-runner.svelte.ts');
	knobs = await vite.ssrLoadModule('/src/lib/rb/knob-control.svelte.ts');
	audioEngine = await vite.ssrLoadModule('/src/lib/rb/audio-engine.svelte.ts');
	performanceIpc = await vite.ssrLoadModule('/src/lib/rb/performance-ipc.svelte.ts');
	uninstallPerformanceIpc = performanceIpc.installPerformanceBrowserIpc();
});

after(async () => {
	try {
		uninstallPerformanceIpc?.();
		delete globalThis.window;
		delete globalThis.HTMLElement;
	} finally {
		await vite?.close();
	}
});

beforeEach(() => {
	glue._resetControllerStateForTests();
	knobs._resetKnobRegistryForTests();
	runner._resetCommandedMovesForTests();
	const { mixerState, deckStates } = audioEngine;
	for (const deck of [1, 2, 3, 4]) {
		mixerState.channels[deck].stem_eq_mode = false;
		deckStates[deck].stems = { ...deckStates[deck].stems, status: 'idle' };
	}
	t = 1000;
	pending = [];
	runner._setCommandedMoveClockForTests({
		now: () => t,
		nextFrame: () =>
			new Promise((resolve) => {
				pending.push(resolve);
			})
	});
});

async function step(ms) {
	t += ms;
	const rs = pending;
	pending = [];
	for (const r of rs) r();
	await new Promise((resolve) => setImmediate(resolve));
}

function registerMixerKnob(id, read, write, runnerWrites) {
	knobs.registerKnob({
		id,
		getValue: read,
		setValue: (x) => {
			runnerWrites.push(x);
			write(x);
		}
	});
}

/** @param {string[]} available */
function readyStems(available) {
	const controls = {};
	for (const stem of ['vocal', 'instrumental', 'drums', 'bass', 'other']) {
		controls[stem] = { muted: false, solo: false, gain: 0.5 };
	}
	return {
		status: 'ready',
		source: 'demucs',
		model: 'htdemucs',
		layout: 'demucs4',
		available_controls: available,
		controls,
		error: null,
		load: null
	};
}

// [if] a move runs on 2:low and MIDI EQ-low for deck 2 arrives [then] cancel midi same tick.
test('MIDI EQ-low on deck 2 cancels the move on knobId(2, low)', async () => {
	const { mixerState } = audioEngine;
	mixerState.channels[2].eq_low = 1;
	const runnerWrites = [];
	registerMixerKnob(
		'2:low',
		() => mixerState.channels[2].eq_low,
		(v) => {
			mixerState.channels[2].eq_low = v;
		},
		runnerWrites
	);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('2:low', (from, now) => linearPlan(from, 0, now, endMs));
	assert.ok(handle);
	await step(16);
	await step(16);
	const writesBeforeMidi = runnerWrites.length;
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 2, target: 'eq', band: 'low' },
		{ kind: 'continuous', value01: 0.3, raw: 38 }
	);
	const rec = runner.commandedMoveLog().at(-1);
	assert.deepEqual(
		{ control_id: rec.control_id, outcome: rec.outcome, reason: rec.reason },
		{ control_id: '2:low', outcome: 'cancelled', reason: 'midi' }
	);
	const result = await handle.done;
	assert.equal(result.outcome, 'cancelled');
	await new Promise((resolve) => setImmediate(resolve));
	await step(80);
	assert.equal(mixerState.channels[2].eq_low, 0.3);
	assert.equal(runnerWrites.length, writesBeforeMidi);
});

// [if] deck 1 fader move runs and MIDI moves deck 3 fader [then] deck 1 move completes.
test('deck 3 fader MIDI leaves deck 1 fader move running', async () => {
	const { mixerState } = audioEngine;
	mixerState.channels[1].fader = 0.2;
	mixerState.channels[3].fader = 0;
	const id1 = knobs.faderControlId(1);
	const runnerWrites = [];
	registerMixerKnob(
		id1,
		() => mixerState.channels[1].fader,
		(v) => {
			mixerState.channels[1].fader = v;
		},
		runnerWrites
	);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove(id1, (from, now) => linearPlan(from, 0.8, now, endMs));
	assert.ok(handle);
	await step(16);
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 3, target: 'fader' },
		{ kind: 'continuous14', value01: 0.5, raw: 8192 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.ok(runner.activeCommandedMoveIds().includes(id1));
	assert.equal(runner.commandedMoveLog().length, 0);
	while (pending.length > 0) await step(16);
	const result = await handle.done;
	assert.equal(result.outcome, 'completed');
	assert.equal(runner.commandedMoveLog().at(-1)?.reason, 'completed');
	assert.equal(mixerState.channels[3].fader, 0.5);
	assert.ok(mixerState.channels[1].fader >= 0.75);
});

test('deck 1 fader MIDI cancels deck 1 fader move with reason midi', async () => {
	const { mixerState } = audioEngine;
	mixerState.channels[1].fader = 0.2;
	const id1 = knobs.faderControlId(1);
	const runnerWrites = [];
	registerMixerKnob(
		id1,
		() => mixerState.channels[1].fader,
		(v) => {
			mixerState.channels[1].fader = v;
		},
		runnerWrites
	);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove(id1, (from, now) => linearPlan(from, 0.8, now, endMs));
	await step(16);
	const n = runnerWrites.length;
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 1, target: 'fader' },
		{ kind: 'continuous14', value01: 0.55, raw: 9000 }
	);
	assert.equal(runner.commandedMoveLog().at(-1)?.reason, 'midi');
	await handle.done;
	await new Promise((resolve) => setImmediate(resolve));
	await step(80);
	assert.equal(mixerState.channels[1].fader, 0.55);
	assert.equal(runnerWrites.length, n);
});

// [if] deck 2 trim move runs and MIDI trim arrives [then] log shows cancelled midi end_ms finite.
test('MIDI trim on deck 2 cancels commanded move on knobId(2, trim)', async () => {
	const { mixerState } = audioEngine;
	mixerState.channels[2].trim = 0.6;
	const trimId = knobs.knobId(2, 'trim');
	const runnerWrites = [];
	registerMixerKnob(
		trimId,
		() => mixerState.channels[2].trim,
		(v) => {
			mixerState.channels[2].trim = v;
		},
		runnerWrites
	);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove(trimId, (from, now) =>
		linearPlan(from, 0.1, now, endMs)
	);
	assert.ok(handle);
	await step(16);
	const writesBeforeMidi = runnerWrites.length;
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 2, target: 'trim' },
		{ kind: 'continuous', value01: 0.75, raw: 95 }
	);
	const rec = runner.commandedMoveLog().at(-1);
	assert.deepEqual(
		{ control_id: rec.control_id, outcome: rec.outcome, reason: rec.reason },
		{ control_id: '2:trim', outcome: 'cancelled', reason: 'midi' }
	);
	assert.ok(Number.isFinite(rec.actual_end_ms));
	const result = await handle.done;
	assert.equal(result.outcome, 'cancelled');
	await new Promise((resolve) => setImmediate(resolve));
	await step(80);
	assert.equal(mixerState.channels[2].trim, 0.75);
	assert.equal(runnerWrites.length, writesBeforeMidi);
});

// [if] stem-EQ move on vocal dial runs and MIDI EQ-high arrives [then] cancel midi on stem-vocal.
test('stem-EQ mode: MIDI EQ-high cancels move on the vocal stem dial', async () => {
	const { mixerState, deckStates } = audioEngine;
	mixerState.channels[2].stem_eq_mode = true;
	deckStates[2].stems = readyStems(['vocal', 'instrumental', 'drums']);
	const stemId = knobs.knobId(2, knobs.stemKnobRole('vocal'));
	const runnerWrites = [];
	registerMixerKnob(
		stemId,
		() => deckStates[2].stems.controls.vocal.gain,
		(v) => {
			deckStates[2].stems.controls.vocal.gain = v;
		},
		runnerWrites
	);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove(stemId, (from, now) =>
		linearPlan(from, 0.2, now, endMs)
	);
	await step(16);
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 2, target: 'eq', band: 'high' },
		{ kind: 'continuous', value01: 0.35, raw: 45 }
	);
	const rec = runner.commandedMoveLog().at(-1);
	assert.deepEqual(
		{ control_id: rec.control_id, outcome: rec.outcome, reason: rec.reason },
		{ control_id: '2:stem-vocal', outcome: 'cancelled', reason: 'midi' }
	);
	await handle.done;
});

// [if] hp-level move runs and headphone_level MIDI arrives [then] cancel midi on hp:hp-level.
test('headphone level MIDI cancels move on hp:hp-level', async () => {
	const { mixerState } = audioEngine;
	mixerState.headphones.level = 0.5;
	const hpLevelId = knobs.knobId('hp', 'hp-level');
	const runnerWrites = [];
	registerMixerKnob(
		hpLevelId,
		() => mixerState.headphones.level,
		(v) => {
			mixerState.headphones.level = v;
		},
		runnerWrites
	);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove(hpLevelId, (from, now) =>
		linearPlan(from, 0.2, now, endMs)
	);
	await step(16);
	glue.handleMidiAction(
		{ type: 'headphone_level' },
		{ kind: 'continuous', value01: 0.75, raw: 95 }
	);
	const rec = runner.commandedMoveLog().at(-1);
	assert.deepEqual(
		{ control_id: rec.control_id, outcome: rec.outcome, reason: rec.reason },
		{ control_id: 'hp:hp-level', outcome: 'cancelled', reason: 'midi' }
	);
	await handle.done;
	await new Promise((resolve) => setImmediate(resolve));
	await step(80);
	assert.equal(mixerState.headphones.level, 0.75);
});

test('no active move: MIDI dispatches exactly as before', async () => {
	const { mixerState } = audioEngine;
	mixerState.channels[2].trim = 0;
	mixerState.channels[2].eq_low = 0;
	mixerState.channels[4].fader = 0;
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 2, target: 'trim' },
		{ kind: 'continuous', value01: 0.75, raw: 95 }
	);
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 2, target: 'eq', band: 'low' },
		{ kind: 'continuous', value01: 0.1, raw: 13 }
	);
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 4, target: 'fader' },
		{ kind: 'continuous14', value01: 0.5, raw: 8192 }
	);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(mixerState.channels[2].trim, 0.75);
	assert.equal(mixerState.channels[2].eq_low, 0.1);
	assert.equal(mixerState.channels[4].fader, 0.5);
	assert.equal(runner.commandedMoveLog().length, 0);
	knobs.noteKnobHandInput('2:low', 'midi');
	assert.equal(runner.commandedMoveLog().length, 0);
});

test('filter and headphone paths report MIDI cancel', async () => {
	const { mixerState } = audioEngine;
	mixerState.channels[2].filter = 0.4;
	mixerState.headphones.mix = 0.6;
	registerMixerKnob(
		'2:filter',
		() => mixerState.channels[2].filter,
		(v) => {
			mixerState.channels[2].filter = v;
		},
		[]
	);
	registerMixerKnob(
		knobs.knobId('hp', 'hp-mix'),
		() => mixerState.headphones.mix,
		(v) => {
			mixerState.headphones.mix = v;
		},
		[]
	);
	const endMs = t + 500;
	const hFilter = runner.startCommandedMove('2:filter', (from, now) =>
		linearPlan(from, 0.1, now, endMs)
	);
	const hHp = runner.startCommandedMove(knobs.knobId('hp', 'hp-mix'), (from, now) =>
		linearPlan(from, 0.2, now, endMs)
	);
	await step(16);
	glue.handleMidiAction(
		{ type: 'mixer_channel', deck: 2, target: 'filter' },
		{ kind: 'continuous', value01: 0.7, raw: 89 }
	);
	glue.handleMidiAction({ type: 'headphone_mix' }, { kind: 'continuous', value01: 0.8, raw: 102 });
	assert.equal(runner.commandedMoveLog().find((r) => r.control_id === '2:filter')?.reason, 'midi');
	assert.equal(runner.commandedMoveLog().find((r) => r.control_id === 'hp:hp-mix')?.reason, 'midi');
	await Promise.all([hFilter.done, hHp.done]);
});

test('source guard: _handleMixerChannel wires noteKnobHandInput midi', () => {
	const source = readFileSync(join(FE_ROOT, 'src/lib/rb/midi/action-glue.svelte.ts'), 'utf8');
	const start = source.indexOf('function _handleMixerChannel(');
	const tail = source.slice(start);
	const end = tail.indexOf('\nfunction _handleMixerGlobal');
	const body = end < 0 ? tail : tail.slice(0, end);
	assert.match(body, /noteKnobHandInput\(/);
	assert.match(body, /'midi'/);
});

test('color_fx press on a loaded deck 1 / filter dispatches; release does not', async () => {
	const deck = audioEngine.deckStates[1];
	deck.stable_id = 'a'.repeat(40);
	try {
		await performanceIpc.dispatchPerformanceCommand({ type: 'color_fx', deck: 1, effect: 'space' });
		assert.equal(performanceIpc.queryPerformanceState().color_fx[1].effect, 'space');
		glue.handleMidiAction(
			{ type: 'color_fx', deck: 1, effect: 'filter' },
			{ kind: 'button', pressed: true, velocity: 127 }
		);
		await new Promise((resolve) => setImmediate(resolve));
		assert.equal(performanceIpc.queryPerformanceState().color_fx[1].effect, 'filter');
		await performanceIpc.dispatchPerformanceCommand({ type: 'color_fx', deck: 1, effect: 'space' });
		glue.handleMidiAction(
			{ type: 'color_fx', deck: 1, effect: 'filter' },
			{ kind: 'button', pressed: false, velocity: 0 }
		);
		await new Promise((resolve) => setImmediate(resolve));
		assert.equal(performanceIpc.queryPerformanceState().color_fx[1].effect, 'space');
	} finally {
		deck.stable_id = null;
	}
});

// [if] LOOP IN is pending and LOOP EXIT is pressed [then] pending_loop_in_ms is cleared.
test('deck_loop_exit clears pending_loop_in_ms so LOOP OUT cannot use the cancelled in point', async () => {
	const deck = audioEngine.deckStates[1];
	deck.stable_id = 'b'.repeat(40);
	deck.pending_loop_in_ms = 1234;
	const realSetLoop = audioEngine.engine.setLoop;
	audioEngine.engine.setLoop = async () => {};
	try {
		glue.handleMidiAction(
			{ type: 'deck_loop_exit', deck: 1 },
			{ kind: 'button', pressed: false, velocity: 0 }
		);
		assert.equal(deck.pending_loop_in_ms, 1234, 'release does not clear');
		glue.handleMidiAction(
			{ type: 'deck_loop_exit', deck: 1 },
			{ kind: 'button', pressed: true, velocity: 127 }
		);
		await new Promise((resolve) => setImmediate(resolve));
		assert.equal(deck.pending_loop_in_ms, null);
	} finally {
		audioEngine.engine.setLoop = realSetLoop;
		deck.stable_id = null;
		deck.pending_loop_in_ms = null;
	}
});
