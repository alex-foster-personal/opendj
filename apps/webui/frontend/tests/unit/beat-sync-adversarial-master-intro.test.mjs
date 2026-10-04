/**
 * ADVERSARIAL (finding F7; was RED, FIXED by computeFollowerSyncPlan's
 * master-intro extrapolation): Rust engine mode's page-decided Beat Sync
 * (`rust-transport.ts`) driven through the same recording stand-in engine as
 * rust-sync.test.mjs, plus the shared join plan both engines use.
 *
 * 4. A synced PLAY was refused while the master was in its pre-first-beat
 *    intro (`_enclosingBeatIndex` threw for a position before beats[0].t),
 *    so the follower did not start at all.
 *
 * Chosen behavior: in BEAT mode the master's grid is extrapolated BACKWARDS
 * at its first interval, so the follower starts where the master's first
 * beat will meet it in phase (the intro of a track is almost always at its
 * opening tempo, and the phase lock measures nothing until the master enters
 * its grid, so a wrong guess costs a trim, never a re-seek loop). BAR mode
 * still refuses: an extrapolated beat has no real downbeat number, the same
 * rule BAR already applies to extrapolated anchors.
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
let sent;
let positions;

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/rust-mode-entry.ts');
});

beforeEach(() => {
	sent = [];
	positions = {};
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt: new Set(),
		send: (cmd) => {
			sent.push(cmd);
			return Promise.resolve({ type: 'result', id: null, ok: true });
		},
		positionMs: (deck) => positions[deck] ?? null
	});
	m.rustMode.enabled = true;
	m.rustMode.keyLock = false;
	m.rustMaster.deck = null;
	m.rustMaster.mode = 'auto';
	m.uiPrefs.beat_sync_max = false;
	for (const d of [1, 2, 3, 4]) {
		const st = m.deckStates[d];
		Object.assign(st, {
			stable_id: null,
			playing: false,
			audible: false,
			is_master: false,
			anlz: null,
			loop: null,
			cue_ms: null,
			position_ms: 0,
			pitch: 1,
			beat_sync_enabled: false,
			quantize_enabled: false,
			quantize_grid_beats: 1,
			sync_mode: 'beat',
			sync_error: null
		});
		m.pitchRanges[d] = 8;
		m.mixerState.channels[d].fader = 1;
		m.mixerState.channels[d].trim = 0.5;
		m.mixerState.channels[d].assign = 'THRU';
	}
	m.mixerState.crossfader = 0.5;
	m.mixerState.master = 1;
});

after(() => {
	m.installRustEngineClientForTest(null);
	m.rustMode.enabled = false;
});

function grid(bpm, startSec, count) {
	const spb = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm, t: startSec + i * spb }));
}

function loadDeck(deck, beats, extra = {}) {
	Object.assign(m.deckStates[deck], {
		stable_id: `track-${deck}`,
		duration_ms: beats.at(-1).t * 1000,
		anlz: { beatgrid: { source: 'rekordbox', beats, beat_count: beats.length, status: 'ok' } },
		...extra
	});
}


async function playingMaster(deck, beats, atMs) {
	loadDeck(deck, beats);
	await m.executeInRustEngine({ type: 'play', deck, playing: true });
	positions[deck] = atMs;
	assert.equal(m.rustMaster.deck, deck);
	sent = [];
}

test('4. a synced follower can start while the master is in its pre-first-beat intro', async () => {
	// Master's first beat at 0.5 s (silence/pickup before it), playing at 0.2 s.
	await playingMaster(1, grid(128, 0.5, 1200), 200);
	loadDeck(2, grid(126, 0.05, 1200), { beat_sync_enabled: true, position_ms: 30_000 });
	// Observed on the unfixed code: rejects with "master position 0.22 is
	// outside the beat grid interval [0.5, ...)" and deck 2 never starts.
	await m.executeInRustEngine({ type: 'play', deck: 2, playing: true });
	assert.equal(m.deckStates[2].playing, true);
});

// ------------------------------------------------- the shared join plan

let bsm;
before(async () => {
	bsm = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
});

function plan(masterGrid, followerGrid, masterSec, followerSec, mode = 'beat', masterTempoRatio = 1) {
	return bsm.computeFollowerSyncPlan({
		masterGrid,
		followerGrid,
		masterPositionAtSyncSec: masterSec,
		masterTempoRatio,
		followerPositionSec: followerSec,
		currentContextTimeSec: 0,
		syncAtContextTimeSec: 0.1,
		minFollowerTempoRatio: 0.92,
		maxFollowerTempoRatio: 1.08,
		mode
	});
}

for (const [label, masterBpm, followerBpm, normalization] of [
	['same tempo', 128, 126, 1],
	['double-tempo follower', 87, 174, 2],
	['half-tempo follower', 174, 87, 0.5]
]) {
	test(`intro join (${label}): the follower meets the master's first beat in phase, as an in-grid join would`, () => {
		const master = grid(masterBpm, 0.5, 600);
		const follower = grid(followerBpm, 0.05, 1200);
		for (const introSec of [0.05, 0.22, 0.49]) {
			const p = plan(master, follower, introSec, 30);
			assert.equal(p.tempoNormalization, normalization, 'precondition: fold');
			assert.ok(p.masterBeatIndex < 0, 'an extrapolated (negative) master beat');
			// Play both forward to the master's first real beat.
			const followerThere = p.followerPositionSec + (master[0].t - introSec) * p.followerTempoRatio;
			// The in-grid join made AT that beat, from there, is the reference.
			const reference = plan(master, follower, master[0].t, followerThere);
			assert.ok(
				Math.abs(reference.followerPositionSec - followerThere) < 1e-6,
				`${introSec}: off by ${((followerThere - reference.followerPositionSec) * 1000).toFixed(2)} ms`
			);
			assert.ok(Math.abs(reference.followerTempoRatio - p.followerTempoRatio) < 1e-9);
		}
	});
}

test('control: BAR mode still refuses an intro anchor (no real downbeat to count from)', () => {
	assert.throws(
		() => plan(grid(128, 0.5, 600), grid(126, 0.05, 600), 0.22, 30, 'bar'),
		new RegExp(bsm.BAR_SYNC_EXTRAPOLATED_ANCHOR)
	);
});

test('control: inside the grid the join is unchanged (a real beat index, a real beat number)', () => {
	const master = grid(128, 0.5, 600);
	const p = plan(master, grid(126, 0.05, 600), 60, 30);
	const i = Math.floor((60 - 0.5) / (60 / 128));
	assert.equal(p.masterBeatIndex, i);
	assert.equal(p.masterBeatNumber, master[i].n);
	assert.ok(Math.abs(p.beatPhase - ((60 - master[i].t) / (60 / 128))) < 1e-9);
	// Past the LAST beat is still refused: an outro is not extrapolated.
	assert.throws(() => plan(master, grid(126, 0.05, 600), master.at(-1).t + 0.1, 30), /outside the beat grid/);
});


test('control: an intro beat counts its number BACKWARDS from the first beat, and its phase too', () => {
	// First beat (n=1) at 0.5 s, 0.5 s apart (120 BPM): 0.3 s is 0.6 of the way
	// through the beat before it (from 0.0 s), which counts as n=4.
	const master = grid(120, 0.5, 600);
	const p = plan(master, grid(126, 0.05, 600), 0.3, 30);
	assert.equal(p.masterBeatIndex, -1);
	assert.equal(p.masterBeatNumber, 4);
	assert.ok(Math.abs(p.beatPhase - 0.6) < 1e-9, `phase ${p.beatPhase}`);
	const two = plan(master, grid(126, 0.05, 600), 0.05, 30);
	assert.equal(two.masterBeatIndex, -1);
	const back2 = plan(grid(120, 1.5, 600), grid(126, 0.05, 600), 0.3, 30);
	assert.equal(back2.masterBeatIndex, -3);
	assert.equal(back2.masterBeatNumber, 2);
	assert.ok(Math.abs(back2.beatPhase - 0.6) < 1e-9, `phase ${back2.beatPhase}`);
});
