/** Transition preset commands (#9456). */
import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const presets = await loadTypeScriptModule('src/lib/rb/transition-presets.ts');
const registry = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');

function grid(n, beatMs = 500) {
	return Array.from({ length: n }, (_, i) => ({
		n: (i % 4) + 1,
		time_ms: i * beatMs
	}));
}

function deck(id, overrides = {}) {
	const controls = { low: 0.5, mid: 0.5, high: 0.5, filter: 0.5, fader: 1, ...overrides.controls };
	return {
		id,
		loaded: true,
		playing: true,
		isMaster: false,
		position_ms: 0,
		beatgrid: grid(64),
		controls,
		...overrides,
		controls
	};
}

function twoDeckHost(decks, extraInput = {}) {
	const state = { decks: [...decks], extraInput };
	return fakeHost(state);
}

function fakeHost(state) {
	let tickFn = null;
	let cancel = () => {
		tickFn = null;
	};
	const host = {
		writes: [],
		scheduled: false,
		write(c) {
			host.writes.push(c);
		},
		schedule(fn) {
			host.scheduled = true;
			tickFn = fn;
			return () => {
				tickFn = null;
				cancel = () => {};
			};
		},
		step(posMs) {
			const master = state.decks.find((d) => d.isMaster) ?? state.decks[0];
			master.position_ms = posMs;
			if (tickFn) tickFn();
		},
		capturedTick() {
			return tickFn;
		},
		readDecks() {
			return { decks: state.decks, ...state.extraInput };
		}
	};
	return host;
}

beforeEach(() => {
	presets._resetTransitionPresetsForTests();
	registry._resetTransitionPresetsForTests();
});

test('[if] bass-swap-8 on decks 1+2 [then] lows plan to 0 and 0.5, same end beat', () => {
	const d1 = deck(1, { isMaster: true, position_ms: 1250 });
	const d2 = deck(2, { controls: { low: 0, mid: 0.5, high: 0.5, filter: 0.5, fader: 1 } });
	const input = { decks: [d1, d2] };
	const plan = presets.planTransitionPreset(presets.getTransitionPreset('bass-swap-8'), input);
	const outLow = plan.moves.find((m) => m.deck === 1 && m.control === 'low');
	const inLow = plan.moves.find((m) => m.deck === 2 && m.control === 'low');
	assert.equal(outLow.to, 0);
	assert.equal(inLow.to, 0.5);
	assert.equal(plan.startMs, 1500);
	assert.equal(plan.endMs, 5500);
	assert.equal(outLow.endMs, plan.endMs);
	assert.equal(inLow.endMs, plan.endMs);
	assert.ok(d1.beatgrid.some((b) => b.time_ms === plan.endMs));

	const host = twoDeckHost([d1, d2]);
	registry.runCommand('preset.run.bass-swap-8', { timeStamp: 0, transitionPresets: host });

	const beforeSwap = 5000 - 1;
	host.step(beforeSwap);
	const lowsBefore = host.writes.filter((w) => w.type === 'eq' && w.band === 'low');
	assert.equal(lowsBefore.length, 0, 'no low write before beat 7 window');

	host.step(6000);
	const lastLow1 = [...host.writes].reverse().find((w) => w.type === 'eq' && w.deck === 1 && w.band === 'low');
	const lastLow2 = [...host.writes].reverse().find((w) => w.type === 'eq' && w.deck === 2 && w.band === 'low');
	assert.deepEqual(lastLow1, { type: 'eq', deck: 1, band: 'low', value: 0 });
	assert.deepEqual(lastLow2, { type: 'eq', deck: 2, band: 'low', value: 0.5 });
});

test('[if] only one deck playing [then] named error and no control moves', () => {
	const d1 = deck(1, { isMaster: true, position_ms: 1000 });
	const d2 = deck(2, { playing: false });
	const host = twoDeckHost([d1, d2]);

	for (const p of presets.TRANSITION_PRESETS) {
		host.writes.length = 0;
		presets._resetTransitionPresetsForTests();
		assert.throws(
			() => registry.runCommand(`preset.run.${p.id}`, { timeStamp: 0, transitionPresets: host }),
			(err) => err.name === 'TransitionPresetError' && err.code === 'needs_two_playing_decks'
		);
		assert.equal(host.writes.length, 0);
		assert.equal(presets.activeTransitionPresetIds().length, 0);
	}
});

test('[if] preset run twice quickly [then] second aborts and no further writes', async () => {
	const d1 = deck(1, { isMaster: true, position_ms: 1000 });
	const d2 = deck(2);
	const host = twoDeckHost([d1, d2]);

	const first = presets.runTransitionPreset('fader-blend-16', host);
	assert.equal(first.status, 'started');
	host.step(3000);
	assert.ok(host.writes.length > 0);
	const lenAfterMid = host.writes.length;
	const oldTick = host.capturedTick();

	const second = presets.runTransitionPreset('fader-blend-16', host);
	assert.equal(second.status, 'aborted');
	const outcome = await first.done;
	assert.equal(outcome, 'aborted');
	assert.equal(host.capturedTick(), null);

	for (let i = 0; i < 5; i += 1) {
		if (oldTick) oldTick();
	}
	assert.equal(host.writes.length, lenAfterMid);

	const outgoingFader = host.writes.find((w) => w.type === 'fader' && w.deck === 1);
	assert.ok(outgoingFader && outgoingFader.value > 0 && outgoingFader.value < 1);
});

test('[then] every shipped preset id is in the registry with a label', () => {
	for (const p of presets.TRANSITION_PRESETS) {
		const id = `preset.run.${p.id}`;
		const def = registry.PERFORMANCE_COMMAND_REGISTRY.get(id);
		assert.ok(def);
		assert.ok(def.label.length > 0);
		assert.ok(def.label.includes(p.label));
		assert.ok(registry.listPerformanceCommands().some((c) => c.id === id));

		const d1 = deck(1, { isMaster: true, position_ms: 1000 });
		const d2 = deck(2);
		const host = twoDeckHost([d1, d2]);
		presets._resetTransitionPresetsForTests();
		registry.runCommand(id, { timeStamp: 0, transitionPresets: host });
		assert.ok(host.scheduled);
	}
});

test('runCommand rejects unknown id and missing host', () => {
	assert.throws(() => registry.runCommand('preset.run.nope', { timeStamp: 0 }));
	const host = twoDeckHost([deck(1, { isMaster: true }), deck(2)]);
	assert.throws(
		() => registry.runCommand('preset.run.bass-swap-8', { timeStamp: 0 }),
		(err) => err.code === 'no_host'
	);
	assert.equal(host.writes.length, 0);
});

test('three playing decks without hint is ambiguous; hint breaks tie', () => {
	const d1 = deck(1, { isMaster: true, position_ms: 1000 });
	const d2 = deck(2);
	const d3 = deck(3);
	assert.throws(
		() =>
			presets.planTransitionPreset(presets.getTransitionPreset('bass-swap-8'), {
				decks: [d1, d2, d3]
			}),
		(err) => err.code === 'ambiguous_incoming_deck'
	);
	const plan = presets.planTransitionPreset(presets.getTransitionPreset('bass-swap-8'), {
		decks: [d1, d2, d3],
		incomingHint: 3
	});
	assert.equal(plan.incoming, 3);
});

test('non-overlapping presets can run together; shared key aborts prior', () => {
	const d1 = deck(1, { isMaster: true, position_ms: 1000 });
	const d2 = deck(2);
	const host = twoDeckHost([d1, d2]);

	const a = presets.runTransitionPreset('bass-swap-8', host);
	assert.equal(a.status, 'started');
	const b = presets.runTransitionPreset('filter-out-16', host);
	assert.equal(b.status, 'started');
	assert.ok(presets.activeTransitionPresetIds().includes('bass-swap-8'));
	assert.ok(presets.activeTransitionPresetIds().includes('filter-out-16'));
	presets.abortTransitionPreset('bass-swap-8');
	presets.abortTransitionPreset('filter-out-16');
});

test('shipped preset table matches research constants', () => {
	const bass = presets.getTransitionPreset('bass-swap-8');
	const filt = presets.getTransitionPreset('filter-out-16');
	const fade = presets.getTransitionPreset('fader-blend-16');
	assert.deepEqual(
		bass.moves.map((m) => m.to),
		[0.0, 0.5]
	);
	assert.equal(filt.moves[0].to, 0.85);
	assert.deepEqual(
		fade.moves.map((m) => m.to),
		[0.0, 1.0]
	);
	for (const p of presets.TRANSITION_PRESETS) {
		for (const m of p.moves) {
			if (m.startBeat !== undefined) {
				assert.ok(Number.isInteger(m.startBeat));
				assert.ok(m.startBeat >= 0 && m.startBeat < p.beats);
			}
			assert.ok(m.to >= 0 && m.to <= 1);
		}
	}
});

test('[if] master has no grid [then] no_grid and no writes', () => {
	const d1 = deck(1, { isMaster: true, beatgrid: [], position_ms: 0 });
	const d2 = deck(2);
	const host = twoDeckHost([d1, d2]);
	assert.throws(
		() => registry.runCommand('preset.run.bass-swap-8', { timeStamp: 0, transitionPresets: host }),
		(err) => err.code === 'no_grid'
	);
	assert.equal(host.writes.length, 0);
});
