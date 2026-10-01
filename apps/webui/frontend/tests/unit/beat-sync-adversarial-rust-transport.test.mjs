/**
 * ADVERSARIAL (round 2, intentionally RED until fixed): Rust engine mode's
 * page-decided Beat Sync (`rust-transport.ts`) driven through the same
 * recording stand-in engine as rust-sync.test.mjs. What is under test is what
 * the page tells the engine.
 *
 * Findings reproduced here (details and file:line in findings.md):
 *
 * 1. Double-tempo seek loop. An odd-anchored 87 -> 174 BPM join is in phase,
 *    but `phaseLockTick` reads half a master beat of error (phase-lock.ts
 *    `phaseErrorMs`) and re-joins on EVERY state frame: a tempo command per
 *    frame (30/s) and a lock that never trims.
 * 2. An automatic master handoff (master paused, played out, unloaded)
 *    silently ends the phase lock of every remaining follower:
 *    `_lockHolds` drops a lock whose master moved, and nothing re-joins it to
 *    the new master (`electIfAuto` has no follower re-anchor; only the manual
 *    `_setDeckMaster` re-joins). Those decks then free-run - the drift
 *    NAE-19 exists to stop.
 * 3. The trim outlives its lock. When a lock is dropped mid-trim nothing
 *    sends the base back, so the deck keeps playing up to 0.3% off the tempo
 *    sync chose (phase-lock.ts promises "a trim never outlives its error").
 * 4. A synced PLAY is refused while the master is in its pre-first-beat intro
 *    (`_enclosingBeatIndex`, beat-sync-math.ts:141, throws for a position
 *    before beats[0].t), so the follower does not start at all.
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
		m.mixerState.channels[d].assign = 'thru';
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

test('1. an in-phase double-tempo follower is left alone by the phase lock (no per-frame re-join)', async () => {
	const master = grid(87, 0.2, 800);
	const follower = grid(174, 0.1, 1600);
	await playingMaster(1, master, 60_123);
	// Find a follower start that the join anchors on an ODD follower beat:
	// the seek sits 2 * (master beat phase) follower beats past its anchor.
	const masterPhase = beatIndex(master, 60.123 + m.SYNC_LEAD_SEC) % 1;
	let join = null;
	for (const startMs of [30_000, 30_170, 30_350, 30_520]) {
		const j = await joinFollower(2, follower, startMs);
		const anchor = Math.round(beatIndex(follower, j.seekMs / 1000) - 2 * masterPhase);
		if (anchor % 2 === 1) {
			join = j;
			break;
		}
		await m.executeInRustEngine({ type: 'play', deck: 2, playing: false });
		sent = [];
	}
	assert.ok(join, 'precondition: an odd-anchored join was found');
	assert.equal(m.phaseLocksForTest()[2]?.normalization, 2, 'precondition: double-tempo fold');
	// Ground truth: master beat k lands on a follower beat.
	const masterAtSec = 60.123 + m.SYNC_LEAD_SEC;
	const nextMasterBeat = master[Math.ceil(beatIndex(master, masterAtSec))].t;
	const followerThere = join.seekMs / 1000 + (nextMasterBeat - masterAtSec) * join.tempo;
	const fi = beatIndex(follower, followerThere);
	assert.ok(Math.abs(fi - Math.round(fi)) < 1e-6, 'the join is musically in phase');

	// 30 state frames (one second), both decks advancing exactly as planned.
	let rejoins = 0;
	for (let k = 0; k < 30; k++) {
		const dtMs = (k * 1000) / 30;
		positions[1] = masterAtSec * 1000 + dtMs;
		positions[2] = join.seekMs + dtMs * join.tempo;
		m.phaseLockTick();
		await settle();
		rejoins += sent.filter((c) => c.deck === 2).length;
		sent = [];
	}
	// Observed on the unfixed code: 30 commands to deck 2 in 30 frames
	// (one re-join per frame).
	assert.equal(rejoins, 0, `${rejoins} commands sent to an in-phase follower in one second`);
});

test('2. after the master is paused, the remaining synced follower stays phase-locked to the new master', async () => {
	await playingMaster(1, grid(128, 0.1, 1200), 60_000);
	await joinFollower(2, grid(126, 0.05, 1200), 30_000);
	await joinFollower(3, grid(124, 0.3, 1200), 40_000);
	assert.equal(m.phaseLocksForTest()[2]?.master, 1);
	assert.equal(m.phaseLocksForTest()[3]?.master, 1);

	await m.executeInRustEngine({ type: 'play', deck: 1, playing: false });
	const newMaster = m.rustMaster.deck;
	assert.ok(newMaster === 2 || newMaster === 3, `precondition: a follower was elected (${newMaster})`);
	const other = newMaster === 2 ? 3 : 2;
	positions[newMaster] = 31_000;
	positions[other] = 41_000;
	m.phaseLockTick();
	await settle();
	m.phaseLockTick();
	await settle();
	// Observed on the unfixed code: the lock is gone and no join was sent, so
	// deck `other` free-runs against the new master from here on.
	const lock = m.phaseLocksForTest()[other];
	assert.equal(
		lock?.master,
		newMaster,
		`deck ${other} (playing, BEAT SYNC on) has lock ${JSON.stringify(lock ?? null)} after the handoff`
	);
});

test('3. a lock dropped mid-trim sends the base back (no permanent 0.3% offset)', async () => {
	await playingMaster(1, grid(128, 0.1, 1200), 60_000);
	const join = await joinFollower(2, grid(126, 0.05, 1200), 30_000);
	const masterAt = 60_000 + m.SYNC_LEAD_SEC * 1000;
	positions[1] = masterAt;
	positions[2] = join.seekMs - 20 * join.tempo; // 20 ms behind: a capped trim
	m.phaseLockTick();
	await settle();
	const trim = sent.find((c) => c.type === 'tempo' && c.deck === 2);
	assert.ok(trim && trim.ratio > join.tempo, 'precondition: a speed-up trim is in force');
	sent = [];
	// The DJ turns BEAT SYNC off mid-trim (same outcome for a master handoff).
	await m.executeInRustEngine({ type: 'beat_sync', deck: 2, enabled: false });
	m.phaseLockTick();
	await settle();
	const lastTempo = [trim, ...sent.filter((c) => c.type === 'tempo' && c.deck === 2)].at(-1).ratio;
	// Observed on the unfixed code: the last tempo is the trim, ~0.3% fast.
	assert.ok(
		Math.abs(lastTempo - join.tempo) < 1e-12,
		`deck 2 left at ${lastTempo} vs sync base ${join.tempo} ` +
			`(${(((lastTempo / join.tempo) - 1) * 100).toFixed(3)}%)`
	);
});

test('4. a synced follower can start while the master is in its pre-first-beat intro', async () => {
	// Master's first beat at 0.5 s (silence/pickup before it), playing at 0.2 s.
	await playingMaster(1, grid(128, 0.5, 1200), 200);
	loadDeck(2, grid(126, 0.05, 1200), { beat_sync_enabled: true, position_ms: 30_000 });
	// Observed on the unfixed code: rejects with "master position 0.22 is
	// outside the beat grid interval [0.5, ...)" and deck 2 never starts.
	await m.executeInRustEngine({ type: 'play', deck: 2, playing: true });
	assert.equal(m.deckStates[2].playing, true);
});

test('control: a manual master switch re-joins the followers to the new master', async () => {
	await playingMaster(1, grid(128, 0.1, 1200), 60_000);
	await joinFollower(2, grid(126, 0.05, 1200), 30_000);
	await joinFollower(3, grid(124, 0.3, 1200), 40_000);
	positions[2] = 30_000;
	positions[3] = 40_000;
	await m.executeInRustEngine({ type: 'master', deck: 2 });
	assert.equal(m.rustMaster.deck, 2);
	assert.equal(m.phaseLocksForTest()[3]?.master, 2, 'the manual path re-joins');
});
