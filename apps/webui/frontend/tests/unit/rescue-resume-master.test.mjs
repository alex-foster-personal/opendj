/**
 * RESCUE-06: a rescued (reloaded) page resumes with a master deck.
 *
 * The rescue resume starts decks through one shared schedule that skips the
 * play-claim election, so on Mon 5 Oct 2026 the silver preview resumed two
 * decks with no master and AutoPlay queued nothing after them.
 *
 * [if] the rescue resumes decks [then] rescue_resume carries a master_deck that
 *   is one of them [⛔️ if the page resumes with no master].
 * [if] the snapshot master was resumed [then] it stays master [⛔️ if the
 *   operator's master moves to another deck].
 */
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
		beat_stamp: { kind: 'beatgrid', beat_index: 20, beat_n: 1, phase: 0 },
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

function _snapshot(now, masterDeck) {
	return {
		schema: 1,
		captured_at_ms: now - 2_000,
		reason: 'transport',
		app_posture: 'gig',
		master_deck: masterDeck,
		playlist_id: null,
		decks: {
			1: _deck({ deck_id: 1, stable_id: 'track-a' }),
			2: _deck({ deck_id: 2, stable_id: 'track-b' }),
			3: _deck({ deck_id: 3, playing: false, stable_id: null }),
			4: _deck({ deck_id: 4, playing: false, stable_id: null })
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

function _decoded(stableId) {
	return {
		stable_id: stableId,
		duration_ms: 400_000,
		processor_error: null,
		beatgrid_ms: GRID_MS,
		effective_bpm: 128
	};
}

async function _runRestore(masterDeck) {
	const commands = [];
	const now = 1_000_000;
	await restore.runRescuePlaybackRestore(_snapshot(now, masterDeck), {
		now: () => now,
		dispatch: async (command) => {
			commands.push(command);
			return { version: 1 };
		},
		query: () => ({
			version: 1,
			decks: { 1: _decoded('track-a'), 2: _decoded('track-b'), 3: {}, 4: {} }
		}),
		setTimeout: (fn) => {
			fn();
			return 0;
		},
		clearTimeout: () => {},
		requestAnimationFrame: (fn) => {
			fn();
		},
		pushToast: () => {}
	});
	return commands.find((command) => command.type === 'rescue_resume');
}

before(async () => {
	restore = await loadTypeScriptModule('src/lib/rb/performance-rescue-restore.svelte.ts');
	math = await loadTypeScriptModule('src/lib/rb/performance-rescue-math.ts');
});

test('[RESCUE-06] rescueResumeMasterDeck keeps a resumed snapshot master, else the first deck', () => {
	assert.equal(math.rescueResumeMasterDeck(2, [1, 2]), 2);
	assert.equal(math.rescueResumeMasterDeck(null, [1, 2]), 1);
	assert.equal(math.rescueResumeMasterDeck(3, [2, 4]), 2);
	assert.throws(() => math.rescueResumeMasterDeck(1, []), /at least one resumed deck/);
});

test('[RESCUE-06] rescue_resume carries the snapshot master deck', async () => {
	const resume = await _runRestore(2);
	assert.ok(resume, 'rescue_resume was dispatched');
	assert.deepEqual(
		resume.decks.map((entry) => entry.deck),
		[1, 2]
	);
	assert.equal(resume.master_deck, 2);
});

test('[RESCUE-06] rescue_resume still names a master when the snapshot had none', async () => {
	const resume = await _runRestore(null);
	assert.equal(resume.master_deck, 1);
});
