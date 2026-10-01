/**
 * Rust engine mode Beat Sync hardening (adversarial round 5, GREEN): rapid
 * toggles, mutual sync, four decks, partial reach on a master tempo move and
 * malformed tempos, through the same recording stand-in engine as
 * rust-sync.test.mjs. Red Rust findings live in
 * beat-sync-adversarial-rust-transport.test.mjs.
 *
 * Regression lines:
 * - if on/off/on BEAT SYNC leaves more or fewer than one lock, or a lock at a
 *   stale base, then broken
 * - if switching the master between two mutually synced decks leaves the old
 *   master's follower lock in force, or does not re-join the new follower,
 *   then broken
 * - if a master tempo move does not re-join all three followers of a 4-deck
 *   set at the new master tempo then broken
 * - if one follower that cannot reach the new master tempo stops the others
 *   from re-joining, or is left looking locked, then broken
 * - if a NaN, zero or out-of-range tempo reaches the engine then broken
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

const settle = () => new Promise((resolve) => setImmediate(resolve));

async function playingMaster(deck, beats, atMs) {
	loadDeck(deck, beats);
	await m.executeInRustEngine({ type: 'play', deck, playing: true });
	positions[deck] = atMs;
	assert.equal(m.rustMaster.deck, deck);
	sent = [];
}

/** Fractional beat index of `sec` on `beats` (constant grids only). */
function beatIndex(beats, sec) {
	return (sec - beats[0].t) / (beats[1].t - beats[0].t);
}

/** Join `deck` to the playing master; returns the planned follower seek. */
async function joinFollower(deck, beats, positionMs) {
	loadDeck(deck, beats, { beat_sync_enabled: true, position_ms: positionMs });
	await m.executeInRustEngine({ type: 'play', deck, playing: true });
	const seek = sent.find((c) => c.type === 'seek' && c.deck === deck);
	const tempo = sent.find((c) => c.type === 'tempo' && c.deck === deck);
	assert.ok(seek && tempo, 'the join sent tempo and seek');
	sent = [];
	return { seekMs: seek.position_ms, tempo: tempo.ratio };
}


test('on/off/on BEAT SYNC leaves exactly one lock, at the last join base', async () => {
	await playingMaster(1, grid(128, 0.1, 1200), 60_000);
	await joinFollower(2, grid(126, 0.05, 1200), 30_000);
	positions[2] = 30_500;
	await m.executeInRustEngine({ type: 'beat_sync', deck: 2, enabled: false });
	assert.equal(m.deckStates[2].beat_sync_enabled, false);
	m.phaseLockTick();
	assert.equal(m.phaseLocksForTest()[2], undefined, 'off drops the lock on the next frame');
	await m.executeInRustEngine({ type: 'beat_sync', deck: 2, enabled: true });
	const tempo = sent.find((c) => c.type === 'tempo' && c.deck === 2);
	assert.ok(tempo, 're-enabling re-joins');
	const lock = m.phaseLocksForTest()[2];
	assert.equal(lock?.master, 1);
	assert.equal(lock?.base, tempo.ratio);
	assert.equal(Object.keys(m.phaseLocksForTest()).length, 1);
});

test('mutual sync: switching the master between two synced decks re-joins the new follower', async () => {
	await playingMaster(1, grid(128, 0.1, 1200), 60_000);
	m.deckStates[1].beat_sync_enabled = true;
	await joinFollower(2, grid(126, 0.05, 1200), 30_000);
	positions[2] = 30_000;
	await m.executeInRustEngine({ type: 'master', deck: 2 });
	assert.equal(m.rustMaster.deck, 2);
	assert.ok(sent.some((c) => c.type === 'tempo' && c.deck === 1), 'deck 1 now follows deck 2');
	assert.ok(!sent.some((c) => c.deck === 2 && c.type !== 'tempo'), 'the new master is not moved');
	assert.equal(m.phaseLocksForTest()[1]?.master, 2, 'the join records the new lock');
	m.phaseLockTick();
	assert.equal(m.phaseLocksForTest()[2], undefined, 'the old follower lock is gone');
});

test('four decks: a master tempo move re-joins all three followers at the new tempo', async () => {
	await playingMaster(1, grid(128, 0.1, 1200), 60_000);
	await joinFollower(2, grid(126, 0.05, 1200), 30_000);
	await joinFollower(3, grid(124, 0.3, 1200), 40_000);
	await joinFollower(4, grid(130, 0.2, 1200), 50_000);
	Object.assign(positions, { 2: 30_000, 3: 40_000, 4: 50_000 });
	await m.executeInRustEngine({ type: 'tempo', deck: 1, ratio: 1.02 });
	for (const deck of [2, 3, 4]) {
		assert.ok(sent.some((c) => c.type === 'tempo' && c.deck === deck), `deck ${deck} re-joined`);
		assert.equal(m.phaseLocksForTest()[deck]?.masterTempo, 1.02, `deck ${deck} lock at the new tempo`);
	}
});

test('a follower that cannot reach the new master tempo says so; the others still re-join', async () => {
	await playingMaster(1, grid(128, 0.1, 1200), 60_000);
	await joinFollower(2, grid(126, 0.05, 1200), 30_000);
	m.pitchRanges[3] = 10;
	await joinFollower(3, grid(118, 0.3, 1200), 40_000); // needs +8.5% at 1.0
	Object.assign(positions, { 2: 30_000, 3: 40_000 });
	sent = [];
	await m.executeInRustEngine({ type: 'tempo', deck: 1, ratio: 1.03 }); // deck 3 would need +11.7%
	assert.ok(sent.some((c) => c.type === 'tempo' && c.deck === 2), 'deck 2 re-joined');
	assert.equal(sent.some((c) => c.deck === 3), false, 'nothing unreachable was sent to deck 3');
	assert.match(m.deckStates[3].sync_error ?? '', /no phase-capable/);
	assert.equal(m.phaseLocksForTest()[3], undefined, 'deck 3 is not shown as locked');
	assert.equal(m.phaseLocksForTest()[2]?.masterTempo, 1.03);
});

test('NaN, zero and out-of-range tempos never reach the engine', async () => {
	await playingMaster(1, grid(128, 0.1, 1200), 60_000);
	for (const ratio of [Number.NaN, 0, -1, Number.POSITIVE_INFINITY, 1.2]) {
		await assert.rejects(m.executeInRustEngine({ type: 'tempo', deck: 1, ratio }), RangeError, String(ratio));
	}
	assert.deepEqual(sent, []);
});
