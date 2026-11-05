import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * HOTCUE-LOOP-01 - a hot cue set while a loop plays is a LOOP hot cue
 * (rekordbox/CDJ behaviour, mimicked): it stores the loop's start and end,
 * paints in the loop colour, and a press re-engages that loop.
 *
 * Regression lines:
 * - if hot_cue_save drops out_ms on the way to the server then broken
 * - if a save during an engaged loop stores the playhead instead of the loop then broken
 * - if renaming a loop hot cue turns it back into a point cue then broken
 * - if pressing a loop hot cue jumps without engaging the loop then broken
 * - if pressing a point hot cue engages a loop then broken
 * - if a USB-stick session save drops the loop end then broken
 * - if the pad, the controller pad or the Rust engine path skips the loop then broken
 */

globalThis.location ??= { search: '' };

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
let ipc;
let actionsBundle;
let stick;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	actionsBundle = await loadTypeScriptModule('tests/unit/fixtures/hot-cue-loop-entry.ts');
	stick = await loadTypeScriptModule('src/lib/rb/stick-session-edits.ts');
});

function source(relativePath) {
	const text = readFileSync(`${SRC}/${relativePath}`, 'utf8');
	assert.ok(text.length > 0, `${relativePath} read as empty`);
	return text;
}

function saveFetchStub() {
	const bodies = [];
	const original = globalThis.fetch;
	globalThis.fetch = async (input, init = {}) => {
		bodies.push({ url: String(input), method: init.method, body: init.body ? JSON.parse(init.body) : null });
		return Response.json({ cue: null, revision: 'save-revision', reversal: { reversal_id: 'save-token' } });
	};
	return { bodies, restore: () => { globalThis.fetch = original; } };
}

const LOOP_CUE = { slot: 'A', in_ms: 4000, out_ms: 5890, is_loop: true, beat_loop_size: null, color_table_index: null, comment: null };
const POINT_CUE = { ...LOOP_CUE, out_ms: null, is_loop: false };
const BEATS = [
	{ n: 1, bpm: 127, t: 0.135 }, { n: 2, bpm: 127, t: 0.608 }, { n: 3, bpm: 127, t: 1.08 }, { n: 4, bpm: 127, t: 1.553 },
	{ n: 1, bpm: 127, t: 2.025 }, { n: 2, bpm: 127, t: 2.497 }, { n: 3, bpm: 127, t: 2.97 }, { n: 4, bpm: 127, t: 3.442 }
];

function triggerDriver(cue, { playing = true, loopEngaged = false } = {}) {
	const calls = [];
	return {
		calls,
		driver: {
			stableId: () => 'loaded-track',
			refresh: async () => {},
			triggerState: () => ({ cue, playing, loopEngaged, positionSec: 1.8, beats: BEATS }),
			jump: async (deck, positionMs) => { calls.push(['jump', deck, positionMs]); },
			arm: async (deck, positionMs) => { calls.push(['arm', deck, positionMs]); return 11; },
			engageLoop: async (deck, loop) => { calls.push(['engageLoop', deck, loop]); },
			contextTimeNowSec: () => 10
		}
	};
}

// ----------------------------------------------------------------- IPC save

test('hot_cue_save with out_ms sends the loop end to the server, unsnapped', async () => {
	globalThis.window = {};
	const originalBeatSyncMax = ipc.uiPrefs.beat_sync_max;
	ipc.uiPrefs.beat_sync_max = false;
	const { bodies, restore } = saveFetchStub();
	const resetDriver = ipc.installPerformanceHotCueDriverForTest({ stableId: () => 'loaded-track', refresh: async () => {} });
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await window.musicDjToolsPerformance.dispatch({
			type: 'hot_cue_save', deck: 1, slot: 'A', in_ms: 4000, out_ms: 5890, revision: 'etag'
		});
		assert.equal(bodies.length, 1);
		assert.equal(bodies[0].method, 'PUT');
		assert.equal(bodies[0].body.in_ms, 4000);
		assert.equal(bodies[0].body.out_ms, 5890, 'the loop end must reach PUT /hot-cues/{slot}');
		await window.musicDjToolsPerformance.dispatch({
			type: 'hot_cue_save', deck: 1, slot: 'B', in_ms: 4000, revision: 'etag'
		});
		assert.equal(bodies[1].body.out_ms, null, 'a point cue save sends out_ms null');
	} finally {
		uninstall();
		resetDriver();
		restore();
		delete globalThis.window;
		ipc.uiPrefs.beat_sync_max = originalBeatSyncMax;
	}
});

test('hot_cue_save refuses a loop end at or before its start', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		for (const out_ms of [4000, 3000, 4000.5]) {
			await assert.rejects(
				window.musicDjToolsPerformance.dispatch({
					type: 'hot_cue_save', deck: 1, slot: 'A', in_ms: 4000, out_ms, revision: 'etag'
				}),
				/out_ms must be an integer after in_ms/
			);
		}
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

// -------------------------------------------------------------- IPC trigger

test('pressing a loop hot cue jumps to its start and engages the same loop, even under BeatSyncMax', async () => {
	globalThis.window = {};
	const originalBeatSyncMax = ipc.uiPrefs.beat_sync_max;
	try {
		for (const beatSyncMax of [false, true]) {
			ipc.uiPrefs.beat_sync_max = beatSyncMax;
			const { calls, driver } = triggerDriver(LOOP_CUE);
			const resetDriver = ipc.installPerformanceHotCueDriverForTest(driver);
			const uninstall = ipc.installPerformanceBrowserIpc();
			try {
				const state = await window.musicDjToolsPerformance.dispatch({ type: 'hot_cue_trigger', deck: 1, slot: 'A' });
				assert.deepEqual(calls, [
					['jump', 1, 4000],
					['engageLoop', 1, { in_ms: 4000, out_ms: 5890 }]
				], `beat_sync_max=${beatSyncMax}`);
				assert.equal(state.decks[1].hot_cue_armed, null);
			} finally {
				uninstall();
				resetDriver();
			}
		}
	} finally {
		delete globalThis.window;
		ipc.uiPrefs.beat_sync_max = originalBeatSyncMax;
	}
});

test('pressing a point hot cue never engages a loop', async () => {
	globalThis.window = {};
	const originalBeatSyncMax = ipc.uiPrefs.beat_sync_max;
	ipc.uiPrefs.beat_sync_max = false;
	const { calls, driver } = triggerDriver(POINT_CUE);
	const resetDriver = ipc.installPerformanceHotCueDriverForTest(driver);
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await window.musicDjToolsPerformance.dispatch({ type: 'hot_cue_trigger', deck: 1, slot: 'A' });
		assert.deepEqual(calls, [['jump', 1, 4000]]);
	} finally {
		uninstall();
		resetDriver();
		delete globalThis.window;
		ipc.uiPrefs.beat_sync_max = originalBeatSyncMax;
	}
});

// ------------------------------------------------------------- deck actions

function fakeDeck(overrides = {}) {
	return {
		stable_id: 'loaded-track',
		position_ms: 4321.4,
		quantize_enabled: false,
		anlz: null,
		hot_cues: [],
		hot_cue_revisions: { A: 'rev-a', B: 'rev-b', C: 'rev-c', D: 'rev-d', E: 'rev-e', F: 'rev-f', G: 'rev-g', H: 'rev-h' },
		hot_cue_reversal: null,
		loop: null,
		...overrides
	};
}

async function withActions(deck, run) {
	globalThis.window = {};
	const originalBeatSyncMax = actionsBundle.uiPrefs.beat_sync_max;
	actionsBundle.uiPrefs.beat_sync_max = false;
	const { bodies, restore } = saveFetchStub();
	const resetDriver = actionsBundle.installPerformanceHotCueDriverForTest({ stableId: () => 'loaded-track', refresh: async () => {} });
	const uninstall = actionsBundle.installPerformanceBrowserIpc();
	try {
		await run(actionsBundle.createDeckHotCueActions(() => 1, () => deck), bodies);
	} finally {
		uninstall();
		resetDriver();
		restore();
		delete globalThis.window;
		actionsBundle.uiPrefs.beat_sync_max = originalBeatSyncMax;
	}
}

test('saving a hot cue while a loop is engaged stores that loop, start and end', async () => {
	const deck = fakeDeck({ loop: { in_ms: 4000.2, out_ms: 5889.7, engaged: true, beat_length: 4 } });
	await withActions(deck, async (actions, bodies) => {
		await actions.saveHotCueAt('A');
		assert.equal(bodies[0].body.in_ms, 4000, 'a loop hot cue starts at the loop in, not the playhead');
		assert.equal(bodies[0].body.out_ms, 5890);
	});
});

test('saving with no engaged loop stays a point cue at the playhead', async () => {
	const deck = fakeDeck({ loop: { in_ms: 4000, out_ms: 5890, engaged: false, beat_length: 4 } });
	await withActions(deck, async (actions, bodies) => {
		await actions.saveHotCueAt('A');
		assert.equal(bodies[0].body.in_ms, 4321);
		assert.equal(bodies[0].body.out_ms, null);
	});
});

test('a captured loop passed at click time is saved even after the loop was exited', async () => {
	const deck = fakeDeck();
	await withActions(deck, async (actions, bodies) => {
		await actions.saveHotCueAt('A', 'Drop loop', 4000, false, 'loaded-track', 5890);
		assert.deepEqual([bodies[0].body.in_ms, bodies[0].body.out_ms, bodies[0].body.comment], [4000, 5890, 'Drop loop']);
	});
});

test('renaming a loop hot cue keeps its end', async () => {
	const deck = fakeDeck({ hot_cues: [LOOP_CUE] });
	await withActions(deck, async (actions, bodies) => {
		await actions.renameHotCueAt('A', 4000, 'Build');
		assert.deepEqual([bodies[0].body.in_ms, bodies[0].body.out_ms, bodies[0].body.comment], [4000, 5890, 'Build']);
	});
});

// ------------------------------------------------------------ stick session

test('a USB-stick session save keeps the loop end', () => {
	const slots = stick.withSessionHotCueSlots('usb:stick:1', [
		{ slot: 'A', cue: null, revision: 'r0' }
	]);
	const mutation = stick.saveSessionHotCue('usb:stick:1', 'A', 4000, slots[0].revision, null, 5890);
	assert.equal(mutation.cue.out_ms, 5890);
	assert.equal(mutation.cue.is_loop, true);
});

// ------------------------------------------------------- the other entry points

test('the pad, the controller pad and the Rust engine all carry the loop', () => {
	const bank = source('lib/components/rb/deck/HotCueBank.svelte');
	assert.match(bank, /engagedLoopForHotCue\(deck\)/, 'the pad click must capture the engaged loop when pressed');
	const pads = source('lib/rb/midi/controller-pad-runtime.svelte.ts');
	assert.match(pads, /engagedLoopForHotCue\(deckStates\[deck\]\)/, 'a controller pad must save the engaged loop too');
	const rust = source('lib/audio-engine/rust-engine.ts');
	assert.match(rust, /engageLoop: \(deck, loop\) => executeRustCommand\(\{ type: 'loop', deck, loop, quantize: false \}\)/);
	const engine = source('lib/rb/audio-engine.svelte.ts');
	assert.match(
		engine,
		/st\.loop = st\.loop !== null && st\.loop\.engaged \? st\.loop : displayLoopFrom\(fresh\.cues, fresh\.beatgrid\.beats\);/,
		'saving a hot cue while looping must not drop the engaged loop on refresh'
	);
});
