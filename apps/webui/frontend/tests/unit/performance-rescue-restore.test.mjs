// [RESCUE-02] [RESCUE-03]
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let restore;
let math;

const GRID_MS = Array.from({ length: 128 }, (_, index) => index * (60_000 / 128));

function _deck(overrides = {}) {
	return {
		deck_id: 1,
		stable_id: 'track-a',
		source_path: null,
		position_ms: 10_000,
		playing: true,
		beat_stamp: {
			kind: 'beatgrid',
			beat_index: 20,
			beat_n: 1,
			phase: 0
		},
		pitch: 1,
		pitch_range: 8,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		quantize_enabled: true,
		beat_sync_enabled: true,
		sync_mode: 'bar',
		is_master: false,
		cue_ms: null,
		loop: null,
		hot_cue_armed: null,
		stems: {
			vocal: { muted: false, solo: false, gain: 1 },
			instrumental: { muted: false, solo: false, gain: 1 },
			drums: { muted: false, solo: false, gain: 1 }
		},
		mixer_channel: {
			trim: 0.5,
			eq_high: 0.5,
			eq_mid: 0.5,
			eq_low: 0.5,
			filter: 0.5,
			fader: 1,
			assign: 'THRU',
			cue_enabled: false
		},
		...overrides
	};
}

function _snapshot(now) {
	const deck = _deck();
	return {
		schema: 1,
		captured_at_ms: now - 2_000,
		reason: 'transport',
		app_posture: 'gig',
		master_deck: null,
		playlist_id: null,
		decks: {
			1: { ...deck, deck_id: 1 },
			2: { ...deck, deck_id: 2, playing: false, stable_id: null },
			3: { ...deck, deck_id: 3 },
			4: { ...deck, deck_id: 4, playing: false, stable_id: null }
		},
		mixer: {
			crossfader: 0.5,
			master: 0.5,
			headphones: {
				mix: 0,
				level: 1,
				output_mode: 'practice',
				selected_master_output_device_id: null,
				selected_output_device_id: null
			}
		}
	};
}

before(async () => {
	restore = await loadTypeScriptModule('src/lib/rb/performance-rescue-restore.svelte.ts');
	math = await loadTypeScriptModule('src/lib/rb/performance-rescue-math.ts');
});

test('orchestrator issues one rescue_resume after decode and undo calls rescue_stop_all', async () => {
	const calls = [];
	let now = 1_000_000;
	const dispatch = async (command) => {
		calls.push(command.type);
		return { version: 1 };
	};
	const query = () => ({
		version: 1,
		decks: {
			1: {
				stable_id: 'track-a',
				duration_ms: 120_000,
				processor_error: null,
				beatgrid_ms: GRID_MS,
				effective_bpm: 128
			},
			2: {},
			3: {
				stable_id: 'track-a',
				duration_ms: 120_000,
				processor_error: null,
				beatgrid_ms: GRID_MS,
				effective_bpm: 128
			},
			4: {}
		}
	});
	const toasts = [];
	await restore.runRescuePlaybackRestore(_snapshot(now), {
		now: () => now,
		dispatch,
		query,
		setTimeout: (fn) => {
			fn();
			return 0;
		},
		clearTimeout: () => {},
		requestAnimationFrame: (fn) => {
			fn();
		},
		pushToast: (...args) => {
			toasts.push(args);
		}
	});
	assert.equal(calls.filter((type) => type === 'rescue_resume').length, 1);
	assert.ok(calls.indexOf('rescue_resume') >= 0);
	const lastToast = toasts.at(-1);
	assert.ok(lastToast);
	const action = lastToast[6];
	assert.equal(action?.label, 'Undo');
	action.handler();
	assert.equal(calls.at(-1), 'rescue_stop_all');
});

test('[RESCUE-03] decode wait defers rescue_resume until every playing deck decodes', async () => {
	const calls = [];
	let now = 1_000_000;
	const capturedAt = now - 3_000;
	const deckPlaying = _deck();
	const snapshot = {
		..._snapshot(now),
		captured_at_ms: capturedAt,
		decks: {
			1: { ...deckPlaying, deck_id: 1 },
			2: { ...deckPlaying, deck_id: 2, playing: false, stable_id: null },
			3: { ...deckPlaying, deck_id: 3, stable_id: 'track-b' },
			4: { ...deckPlaying, deck_id: 4, playing: false, stable_id: null }
		}
	};
	let deckThreeDecoded = false;
	const query = () => ({
		version: 1,
		decks: {
			1: {
				stable_id: 'track-a',
				duration_ms: 120_000,
				processor_error: null,
				beatgrid_ms: GRID_MS
			},
			2: {},
			3: deckThreeDecoded
				? {
						stable_id: 'track-b',
						duration_ms: 120_000,
						processor_error: null,
						beatgrid_ms: GRID_MS
					}
				: {
						stable_id: 'track-b',
						duration_ms: null,
						processor_error: null,
						beatgrid_ms: []
					},
			4: {}
		}
	});
	let pollCount = 0;
	const pending = restore.runRescuePlaybackRestore(snapshot, {
		now: () => now,
		dispatch: async (command) => {
			calls.push(command.type);
			return { version: 1 };
		},
		query,
		setTimeout: () => 0,
		clearTimeout: () => {},
		requestAnimationFrame: (fn) => {
			pollCount += 1;
			if (pollCount === 2) deckThreeDecoded = true;
			fn();
		},
		pushToast: () => {}
	});
	assert.equal(calls.filter((type) => type === 'rescue_resume').length, 0);
	await pending;
	assert.equal(calls.filter((type) => type === 'rescue_resume').length, 1);
});

test('[RESCUE-03] 20s ceiling resumes decoded decks and toast names undecoded', async () => {
	const calls = [];
	let now = 1_000_000;
	const capturedAt = now - 5_000;
	const deckPlaying = _deck();
	const snapshot = {
		..._snapshot(now),
		captured_at_ms: capturedAt,
		decks: {
			1: { ...deckPlaying, deck_id: 1 },
			2: { ...deckPlaying, deck_id: 2, playing: false, stable_id: null },
			3: { ...deckPlaying, deck_id: 3, stable_id: 'track-b' },
			4: { ...deckPlaying, deck_id: 4, playing: false, stable_id: null }
		}
	};
	const ceilingCallbacks = [];
	const query = () => ({
		version: 1,
		decks: {
			1: {
				stable_id: 'track-a',
				duration_ms: 120_000,
				processor_error: null,
				beatgrid_ms: GRID_MS
			},
			2: {},
			3: {
				stable_id: 'track-b',
				duration_ms: null,
				processor_error: null,
				beatgrid_ms: []
			},
			4: {}
		}
	});
	const toasts = [];
	const pending = restore.runRescuePlaybackRestore(snapshot, {
		now: () => now,
		dispatch: async (command) => {
			calls.push(command);
			return { version: 1 };
		},
		query,
		setTimeout: (fn, delayMs) => {
			if (delayMs === math.RESCUE_DECODE_CEILING_MS) {
				ceilingCallbacks.push(fn);
				return 1;
			}
			fn();
			return 0;
		},
		clearTimeout: () => {},
		requestAnimationFrame: () => {},
		pushToast: (...args) => {
			toasts.push(args);
		}
	});
	assert.equal(calls.filter((command) => command.type === 'rescue_resume').length, 0);
	assert.equal(ceilingCallbacks.length, 1);
	ceilingCallbacks[0]();
	await pending;
	const resume = calls.find((command) => command.type === 'rescue_resume');
	assert.ok(resume);
	assert.deepEqual(resume.decks.map((entry) => entry.deck), [1]);
	const toastMessage = toasts.at(-1)?.[0];
	assert.match(String(toastMessage), /deck 3: not decoded/);
});
