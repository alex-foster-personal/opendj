import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// requirement: RESCUE-04
// [if] newest snapshot is 3 h old [then] toast is "Restored layout from 3 h ago"
// [if] layout-only restore runs [then] dispatch log contains no play: true
// [if] malformed rescue blob [then] parse returns null

async function _loadRescueSnapshot() {
	return loadTypeScriptModule('src/lib/rb/rescue-snapshot.ts');
}

async function _loadRescueRestore() {
	return loadTypeScriptModule('src/lib/rb/rescue-restore.svelte.ts');
}

async function _loadRescuePlay() {
	return loadTypeScriptModule('src/lib/rb/performance-rescue-play.ts');
}

function _deckSnapshot(deckId, opts) {
	return {
		deck_id: deckId,
		stable_id: opts.stable_id,
		source_path: null,
		playing: opts.playing,
		position_ms: opts.position_ms ?? 0,
		beat_stamp: { kind: 'sample', position_ms: opts.position_ms ?? 0 },
		pitch: 1,
		pitch_range: 8,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		quantize_enabled: true,
		beat_sync_enabled: true,
		sync_mode: 'bar',
		is_master: false,
		cue_ms: opts.position_ms ?? null,
		loop: null,
		hot_cue_armed: null,
		stems: {
			vocal: { muted: false, solo: false, gain: 0.5 },
			instrumental: { muted: false, solo: false, gain: 0.5 },
			drums: { muted: false, solo: false, gain: 0.5 }
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
		}
	};
}

function _buildPayload(decks) {
	return {
		schema: 1,
		captured_at_ms: 1,
		reason: 'periodic',
		app_posture: 'gig',
		master_deck: null,
		playlist_id: null,
		deck_layout: 'more',
		decks,
		mixer: {
			crossfader: 0.5,
			master: 0.8,
			headphones: {
				mix: 0.5,
				level: 0.5,
				output_mode: 'practice',
				selected_master_output_device_id: null,
				selected_output_device_id: null
			}
		}
	};
}

test('formatRescueAgeToast reports 3 h ago', async () => {
	const mod = await _loadRescueRestore();
	const nowMs = 1_700_000_000_000;
	const captured = nowMs - 3 * 60 * 60 * 1000;
	assert.equal(mod.formatRescueAgeToast(captured, nowMs), 'Restored layout from 3 h ago');
});

test('planRescueSimultaneousPlay shares one context time using beat_phase anchors', async () => {
	const mod = await _loadRescuePlay();
	const schedule = mod.planRescueSimultaneousPlay(
		[
			{ deck: 1, beat_phase_ms: 12_000 },
			{ deck: 3, beat_phase_ms: 45_000 }
		],
		10,
		0.05
	);
	assert.equal(schedule.length, 2);
	assert.equal(schedule[0].startAtContextTime, 10.05);
	assert.equal(schedule[1].startAtContextTime, 10.05);
});

test('play restore dispatches scheduled play at shared context time', async () => {
	const snapshotMod = await _loadRescueSnapshot();
	const restoreMod = await _loadRescueRestore();
	const dispatchLog = [];
	const dispatch = async (command) => {
		dispatchLog.push(command);
		return {};
	};
	const payload = _buildPayload({
		1: _deckSnapshot(1, { stable_id: 'sid-a', playing: true, position_ms: 12_000 }),
		2: _deckSnapshot(2, { stable_id: null, playing: false }),
		3: _deckSnapshot(3, { stable_id: 'sid-b', playing: true, position_ms: 45_000 }),
		4: _deckSnapshot(4, { stable_id: null, playing: false })
	});
	const snapshot = snapshotMod.parseRescueSnapshot(payload);
	assert.notEqual(snapshot, null);
	await restoreMod.executeRescueRestore(
		{ snapshot, mode: 'play' },
		{
			dispatch,
			query: () => ({
				decks: {
					1: { playing: true, audible: true, beatgrid: [] },
					2: { playing: false, audible: false, beatgrid: [] },
					3: { playing: true, audible: true, beatgrid: [] },
					4: { playing: false, audible: false, beatgrid: [] }
				}
			}),
			audioContextTime: () => 10
		}
	);
	const playCommands = dispatchLog.filter((command) => command.type === 'play' && command.playing);
	assert.equal(playCommands.length, 2);
	assert.deepEqual(
		playCommands.map((command) => command.start_at_context_sec),
		[10.05, 10.05]
	);
});

test('layout-only restore never dispatches play true', async () => {
	const snapshotMod = await _loadRescueSnapshot();
	const restoreMod = await _loadRescueRestore();
	const dispatchLog = [];
	const dispatch = async (command) => {
		dispatchLog.push(command);
		return {};
	};
	const payload = _buildPayload({
		1: _deckSnapshot(1, { stable_id: 'sid-a', playing: true, position_ms: 1000 }),
		2: _deckSnapshot(2, { stable_id: null, playing: false }),
		3: _deckSnapshot(3, { stable_id: null, playing: false }),
		4: _deckSnapshot(4, { stable_id: null, playing: false })
	});
	const snapshot = snapshotMod.parseRescueSnapshot(payload);
	assert.notEqual(snapshot, null);
	await restoreMod.executeRescueRestore(
		{ snapshot, mode: 'layout' },
		{
			dispatch,
			query: () => ({
				decks: {
					1: { playing: false, audible: false, beatgrid: [] },
					2: { playing: false, audible: false, beatgrid: [] },
					3: { playing: false, audible: false, beatgrid: [] },
					4: { playing: false, audible: false, beatgrid: [] }
				}
			})
		}
	);
	assert.equal(
		dispatchLog.some((command) => command.type === 'play' && command.playing === true),
		false
	);
});

test('malformed rescue blob parse returns null', async () => {
	const mod = await _loadRescueSnapshot();
	assert.equal(mod.parseRescueSnapshot('{not-json'), null);
	assert.equal(mod.parseRescueSnapshot({ schema: 2 }), null);
});
