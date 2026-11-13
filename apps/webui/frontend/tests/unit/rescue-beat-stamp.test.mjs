import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// requirement: RESCUE-01
// [if] a loaded track has a real beatgrid [then] the writer persists a beatgrid stamp that decodes back to the position
// [if] a loaded track has no beatgrid [then] the writer persists its sample-position fallback, never an invented beat stamp
// [if] a deck has no loaded track [then] the writer persists a sample stamp at the deck position

async function _loadBeatStamp() {
	return loadTypeScriptModule('src/lib/rb/rescue-beat-stamp.ts');
}

function _grid120Bpm(seconds = 8) {
	const beats = [];
	for (let i = 0; i < seconds * 2; i += 1) {
		beats.push({ n: (i % 4) + 1, t: i * 0.5 });
	}
	return beats;
}

test('beat stamp round-trips within 1/64 beat on a fixture grid', async () => {
	const { encodeBeatStamp, decodeBeatStamp } = await _loadBeatStamp();
	const beats = _grid120Bpm();
	const beatIntervalMs = 500;
	const tolerance = beatIntervalMs / 64;
	for (const positionMs of [750, 1250, 2100, 3333]) {
		const stamp = encodeBeatStamp(beats, positionMs);
		assert.equal(stamp.kind, 'beatgrid');
		const decoded = decodeBeatStamp(stamp, beats);
		assert.ok(decoded !== null);
		assert.ok(Math.abs(decoded - positionMs) <= tolerance);
	}
});

test('beat stamp falls back to sample when grid is absent', async () => {
	const { encodeBeatStamp, decodeBeatStamp } = await _loadBeatStamp();
	const stamp = encodeBeatStamp([], 1234);
	assert.deepEqual(stamp, { kind: 'sample', position_ms: 1234 });
	assert.equal(decodeBeatStamp(stamp, []), 1234);
});

test('decode returns null when beatgrid stamp is out of grid span', async () => {
	const { decodeBeatStamp } = await _loadBeatStamp();
	const beats = _grid120Bpm(2);
	assert.equal(
		decodeBeatStamp({ kind: 'beatgrid', beat_index: 99, beat_n: 1, phase: 0 }, beats),
		null
	);
});

async function _loadSnapshot() {
	return loadTypeScriptModule('src/lib/rb/rescue-snapshot.ts');
}

function _deckState(overrides) {
	return {
		stable_id: null,
		playing: false,
		position_ms: 0,
		beatgrid: [],
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
			available_controls: ['vocal', 'instrumental', 'drums'],
			controls: {
				vocal: { muted: false, solo: false, gain: 0.5 },
				instrumental: { muted: false, solo: false, gain: 0.5 },
				drums: { muted: false, solo: false, gain: 0.5 }
			}
		},
		...overrides
	};
}

function _performanceState(decks) {
	const channel = {
		trim: 0.5,
		eq_high: 0.5,
		eq_mid: 0.5,
		eq_low: 0.5,
		filter: 0.5,
		fader: 1,
		assign: 'THRU',
		cue_enabled: false
	};
	return {
		master_deck: 1,
		browser: { active_playlist: null },
		decks,
		mixer: {
			crossfader: 0.5,
			master: 0.8,
			channels: { 1: channel, 2: channel, 3: channel, 4: channel },
			headphones: {
				mix: 0,
				level: 0.5,
				output_mode: 'two_outputs',
				outputs: [],
				selected_master_output_device_id: null,
				selected_output_device_id: null
			}
		}
	};
}

function _gridMs(seconds = 8) {
	return _grid120Bpm(seconds).map((beat) => ({ n: beat.n, time_ms: beat.t * 1000 }));
}

async function _capture() {
	const { buildRescueSnapshot } = await _loadSnapshot();
	const state = _performanceState({
		1: _deckState({
			stable_id: 'grid-track',
			playing: true,
			position_ms: 1250,
			beatgrid: _gridMs()
		}),
		2: _deckState({ stable_id: 'no-grid-track', playing: true, position_ms: 1234.4 }),
		3: _deckState({}),
		4: _deckState({ stable_id: 'past-grid-track', position_ms: 9500, beatgrid: _gridMs(2) })
	});
	return buildRescueSnapshot(
		state,
		'periodic',
		1,
		{ 1: '/Users/dev/Music/grid.mp3', 2: '/Users/dev/Music/no-grid.mp3', 3: null, 4: null },
		'more'
	);
}

// A loaded track with a beatgrid persists a beatgrid stamp that decodes back to its position.
test('writer stamps a grid-backed deck with its real beatgrid coordinate', async () => {
	const { decodeBeatStamp } = await _loadBeatStamp();
	const deck = (await _capture()).decks[1];
	assert.deepEqual(deck.beat_stamp, { kind: 'beatgrid', beat_index: 2.5, beat_n: 3, phase: 0.5 });
	assert.equal(deck.position_ms, 1250);
	assert.equal(decodeBeatStamp(deck.beat_stamp, _grid120Bpm()), 1250);
});

// A non-grid track keeps its sample position and never gets an invented beat stamp.
test('writer persists the sample-position fallback for a loaded deck without a beatgrid', async () => {
	const deck = (await _capture()).decks[2];
	assert.deepEqual(deck.beat_stamp, { kind: 'sample', position_ms: 1234 });
	assert.equal(deck.stable_id, 'no-grid-track');
	assert.equal(deck.source_path, '/Users/dev/Music/no-grid.mp3');
	assert.equal('beat_index' in deck.beat_stamp, false);
});

// A position past the last beat cannot be expressed on the grid, so it is saved as a sample stamp.
test('writer falls back to a sample stamp when a gridded deck sits outside its grid span', async () => {
	const deck = (await _capture()).decks[4];
	assert.deepEqual(deck.beat_stamp, { kind: 'sample', position_ms: 9500 });
});

// A deck with no loaded track persists a truthful sample stamp and null identity fields.
test('writer stamps an empty deck with a sample stamp at its position and no track identity', async () => {
	const { decodeBeatStamp } = await _loadBeatStamp();
	const deck = (await _capture()).decks[3];
	assert.deepEqual(deck.beat_stamp, { kind: 'sample', position_ms: 0 });
	assert.equal(deck.stable_id, null);
	assert.equal(deck.source_path, null);
	assert.equal(decodeBeatStamp(deck.beat_stamp, []), 0);
});
