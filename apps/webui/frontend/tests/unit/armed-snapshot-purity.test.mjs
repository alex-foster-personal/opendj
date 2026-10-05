// queryPerformanceState() is a READ path: WaveRow.svelte and Deck.svelte
// evaluate it inside `$derived`, and Svelte 5 throws `state_unsafe_mutation`
// the moment a derived writes to `$state`. The armed-record snapshots used to
// self-clear an expired record (`hotCueArmed[deck] = null`) from inside that
// read, which crashed the preview on demon-llama (webui-client-errors log,
// Thu 1 Oct 2026 05:43:46Z) once an armed hot cue's downbeat had landed.
//
// Under node `$state` is normally an identity function, so a write during a
// read is invisible. This file swaps in a `$state` whose objects refuse
// writes while a simulated derived is evaluating, which is exactly the rule
// Svelte enforces in the browser. Everything else is the real module.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { bundleTypeScriptModule } from './load-typescript.mjs';
import { importBundledSource } from './import-bundled-source.mjs';

let ipc;
let derivedDepth = 0;

function guardedState(value) {
	// Plain records only: a Proxy around a Set or Map breaks its methods'
	// internal-slot receivers, and the armed records are plain objects.
	if (value === null || typeof value !== 'object' || Object.getPrototypeOf(value) !== Object.prototype) {
		return value;
	}
	return new Proxy(value, {
		set(target, key, next) {
			if (derivedDepth > 0) {
				throw new Error(`state_unsafe_mutation: wrote ${String(key)} while a $derived was evaluating`);
			}
			target[key] = next;
			return true;
		},
		deleteProperty(target, key) {
			if (derivedDepth > 0) {
				throw new Error(`state_unsafe_mutation: deleted ${String(key)} while a $derived was evaluating`);
			}
			return delete target[key];
		}
	});
}

/** Run `read` the way a `$derived` would: any $state write throws. */
function asDerived(read) {
	derivedDepth += 1;
	try {
		return read();
	} finally {
		derivedDepth -= 1;
	}
}

before(async () => {
	const text = await bundleTypeScriptModule('tests/unit/fixtures/waveform-seek-session-entry.ts');
	globalThis.__musicDjToolsTestState = guardedState;
	globalThis.__musicDjToolsTestState.snapshot = (value) =>
		value === undefined ? undefined : JSON.parse(JSON.stringify(value));
	ipc = await importBundledSource(text, 'armed-snapshot-purity');
});

const BEATS = Array.from({ length: 64 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 }));

function loadPlayingDeck() {
	const deck = ipc.deckStates[1];
	deck.stable_id = 'purity-deck';
	deck.playing = true;
	deck.loop = null;
	deck.position_ms = 200;
	deck.anlz = { beatgrid: { source: 'rekordbox', beats: BEATS, beat_count: BEATS.length, status: 'ok' } };
}

function unloadDeck() {
	const deck = ipc.deckStates[1];
	deck.stable_id = null;
	deck.playing = false;
	deck.position_ms = 0;
	deck.anlz = null;
}

function clockedDriver(clock, cue = null) {
	return {
		stableId: () => 'purity-deck',
		refresh: async () => {},
		hasRbMapping: () => true,
		triggerState: () => ({ cue, playing: true, loopEngaged: false, positionSec: 0.2, beats: BEATS }),
		jump: async () => {},
		arm: async (_deck, _positionMs, armAt) => {
			const at = typeof armAt === 'function' ? armAt(0.2) : armAt;
			return clock.sec + (at - 0.2);
		},
		contextTimeNowSec: () => clock.sec
	};
}

async function withArmedSession(arm, body) {
	globalThis.window = {};
	const originalBeatSyncMax = ipc.uiPrefs.beat_sync_max;
	ipc.uiPrefs.beat_sync_max = true;
	loadPlayingDeck();
	const clock = { sec: 10 };
	const cue = { slot: 'A', in_ms: 60000, out_ms: null, is_loop: false, beat_loop_size: null, color_table_index: null, comment: null };
	const resetDriver = ipc.installPerformanceHotCueDriverForTest(clockedDriver(clock, cue));
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await arm();
		await body(clock);
	} finally {
		uninstall();
		resetDriver();
		unloadDeck();
		ipc.uiPrefs.beat_sync_max = originalBeatSyncMax;
		delete globalThis.window;
	}
}

test('the guarded $state really does refuse a write during a simulated $derived (control)', () => {
	const probe = guardedState({ a: 1 });
	assert.throws(() => asDerived(() => { probe.a = 2; }), /state_unsafe_mutation/);
	probe.a = 3;
	assert.equal(probe.a, 3, 'outside a derived the same write is allowed');
});

test('an expired armed hot cue reads as null from inside a $derived without writing state', async () => {
	await withArmedSession(
		() => window.musicDjToolsPerformance.dispatch({ type: 'hot_cue_trigger', deck: 1, slot: 'A' }),
		async (clock) => {
			assert.equal(asDerived(() => ipc.queryPerformanceState()).decks[1].hot_cue_armed.slot, 'A');
			clock.sec += 60;
			const state = asDerived(() => ipc.queryPerformanceState());
			assert.equal(state.decks[1].hot_cue_armed, null, 'a landed arm reads as not armed');
		}
	);
});

test('an expired armed waveform seek reads as null from inside a $derived without writing state', async () => {
	await withArmedSession(
		() =>
			window.musicDjToolsPerformance.dispatch({
				type: 'waveform_seek', deck: 1, position_ms: 60000, snap: 'downbeat'
			}),
		async (clock) => {
			assert.equal(
				asDerived(() => ipc.queryPerformanceState()).decks[1].waveform_seek_armed.target_position_ms,
				60000
			);
			clock.sec += 60;
			const state = asDerived(() => ipc.queryPerformanceState());
			assert.equal(state.decks[1].waveform_seek_armed, null, 'a landed arm reads as not armed');
		}
	);
});

test('an expired quantized launch reads as null from inside a $derived without writing state', async () => {
	const launchClock = { sec: 10 };
	const restoreLaunch = ipc.installPerformanceQuantizedLaunchDriverForTest({
		arm: async () => launchClock.sec + 0.5,
		clear: () => {},
		contextTimeNowSec: () => launchClock.sec
	});
	try {
		await withArmedSession(
			() => window.musicDjToolsPerformance.dispatch({ type: 'play', deck: 1, playing: true, quantize: true }),
			async () => {
				assert.ok(asDerived(() => ipc.queryPerformanceState()).decks[1].quantized_launch_armed.remaining_ms > 0);
				launchClock.sec += 60;
				const state = asDerived(() => ipc.queryPerformanceState());
				assert.equal(state.decks[1].quantized_launch_armed, null, 'a launched arm reads as not armed');
			}
		);
	} finally {
		restoreLaunch();
		ipc.resetQuantizedLaunchArmedForTest();
	}
});

test('a play press after a quantized launch has landed is not swallowed as a disarm', async () => {
	const launchClock = { sec: 10 };
	const clearCalls = [];
	const restoreLaunch = ipc.installPerformanceQuantizedLaunchDriverForTest({
		arm: async () => launchClock.sec + 0.5,
		clear: (deck) => clearCalls.push(deck),
		contextTimeNowSec: () => launchClock.sec
	});
	try {
		await withArmedSession(
			() => window.musicDjToolsPerformance.dispatch({ type: 'play', deck: 1, playing: true, quantize: true }),
			async () => {
				launchClock.sec += 60;
				// The expired record is no longer cleared by a read, so the
				// play path itself must tell "armed" from "already launched".
				await window.musicDjToolsPerformance
					.dispatch({ type: 'play', deck: 1, playing: true })
					.catch(() => {});
				assert.deepEqual(clearCalls, [], 'a play press on a landed launch must play, not disarm');
			}
		);
	} finally {
		restoreLaunch();
		ipc.resetQuantizedLaunchArmedForTest();
	}
});
