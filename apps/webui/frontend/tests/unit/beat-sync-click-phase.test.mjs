/**
 * NAE-19 click/hot-cue phase: a waveform click, hot cue or CUE jump on a
 * SYNCED, PLAYING follower lands on the clicked or cued beat, in phase with
 * the master, in one move (JIK, Thu 1 Oct 2026).
 *
 * Both engines already re-joined a seeking follower (Web Audio:
 * `quantizedSeek` -> `_synchronizeFollowers({ followerAnchorSec })`; Rust:
 * `_seek` -> `_join({ followerAtSec })`), but the join picked the anchor whose
 * phase-shifted LANDING was nearest the clicked beat, so with the master past
 * mid-beat the follower landed a beat EARLY (clicked beat - 1 + phase), and
 * in BAR mode up to two beats away. `anchorOnBeat` picks the anchor whose
 * BEAT is nearest the click instead. Masters, paused decks, decks with sync
 * off and beat jumps (already exact, F3) keep their behavior.
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
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

let bsm;
before(async () => {
	bsm = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
});

const MASTER = grid(128, 0.1, 1200);
const FOLLOWER = grid(126, 0.05, 1200);
const MI = (sec) => (sec - MASTER[0].t) / (60 / 128);

function plan(masterSec, followerSec, { mode = 'beat', anchorOnBeat, followerGrid = FOLLOWER } = {}) {
	return bsm.computeFollowerSyncPlan({
		masterGrid: MASTER,
		followerGrid,
		masterPositionAtSyncSec: masterSec,
		masterTempoRatio: 1,
		followerPositionSec: followerSec,
		currentContextTimeSec: 0,
		syncAtContextTimeSec: 0.1,
		minFollowerTempoRatio: 0.92,
		maxFollowerTempoRatio: 1.08,
		mode,
		...(anchorOnBeat === undefined ? {} : { anchorOnBeat })
	});
}

/** Master at fractional beat 400 + phase. */
const masterAt = (phase) => MASTER[400].t + phase * (60 / 128);

for (const phase of [0.1, 0.3, 0.7, 0.9]) {
	test(`BEAT: a click on follower beat 100 with the master at phase ${phase} lands on beat 100 + ${phase}`, () => {
		const p = plan(masterAt(phase), FOLLOWER[100].t, { anchorOnBeat: true });
		assert.equal(p.followerBeatIndex, 100);
		const expected = FOLLOWER[100].t + phase * (FOLLOWER[101].t - FOLLOWER[100].t);
		assert.ok(Math.abs(p.followerPositionSec - expected) < 1e-9, `${p.followerPositionSec} vs ${expected}`);
	});
}

test('control: without anchorOnBeat (every other join) the nearest LANDING still wins: a beat early past mid-beat', () => {
	assert.equal(plan(masterAt(0.7), FOLLOWER[100].t).followerBeatIndex, 99);
	assert.equal(plan(masterAt(0.3), FOLLOWER[100].t).followerBeatIndex, 100);
	assert.equal(plan(masterAt(0.7), FOLLOWER[100].t, { anchorOnBeat: false }).followerBeatIndex, 99);
});

test('BAR: the landing keeps bar alignment with the master, on the bar-aligned beat nearest the click', () => {
	// Master on beat n=1 (index 400) at phase 0.6; the click is on follower beat
	// 101 (n=2): the nearest n=1 beat by BEAT time is 100, not 104 or 96.
	const p = plan(masterAt(0.6), FOLLOWER[101].t, { mode: 'bar', anchorOnBeat: true });
	assert.equal(MASTER[400].n, 1);
	assert.equal(FOLLOWER[p.followerBeatIndex].n, 1);
	assert.equal(p.followerBeatIndex, 100);
	// A click just before n=3 (index 102) is nearer 100; just after, nearer 104.
	assert.equal(plan(masterAt(0.6), FOLLOWER[102].t - 0.01, { mode: 'bar', anchorOnBeat: true }).followerBeatIndex, 100);
	assert.equal(plan(masterAt(0.6), FOLLOWER[102].t + 0.01, { mode: 'bar', anchorOnBeat: true }).followerBeatIndex, 104);
	assert.equal(plan(masterAt(0.6), FOLLOWER[103].t, { mode: 'bar', anchorOnBeat: true }).followerBeatIndex, 104);
});

test('a double-tempo follower lands on the clicked beat too, in phase', () => {
	const fast = grid(256, 0.05, 2400);
	const p = plan(masterAt(0.7), fast[201].t, { anchorOnBeat: true, followerGrid: fast });
	assert.equal(p.tempoNormalization, 2);
	assert.equal(p.followerBeatIndex, 201);
});

// ------------------------------------------------------- Rust mode, end to end

async function playingMaster(deck, beats, atMs) {
	loadDeck(deck, beats);
	await m.executeInRustEngine({ type: 'play', deck, playing: true });
	positions[deck] = atMs;
	sent = [];
}

async function playingFollower(deck, beats, atMs) {
	loadDeck(deck, beats, { beat_sync_enabled: true, position_ms: atMs });
	await m.executeInRustEngine({ type: 'play', deck, playing: true });
	positions[deck] = atMs;
	sent = [];
}

/** The master's beat phase where the join plans it (one lead ahead). */
function masterPhaseAtJoin(masterMs) {
	const b = MI(masterMs / 1000 + m.SYNC_LEAD_SEC);
	return b - Math.floor(b);
}

test('Rust: a waveform click on a synced playing follower seeks to the clicked beat + the master phase', async () => {
	// Master at phase ~0.7 when the join lands.
	const masterMs = (masterAt(0.7) - m.SYNC_LEAD_SEC) * 1000;
	await playingMaster(1, MASTER, masterMs);
	await playingFollower(2, FOLLOWER, 30_000);
	m.deckStates[2].quantize_enabled = true;
	positions[1] = masterMs;
	const clicked = FOLLOWER[100].t * 1000 + 40; // a click just after beat 100 (snaps to it)
	await m.executeInRustEngine({ type: 'seek', deck: 2, position_ms: clicked });
	const seek = sent.find((c) => c.type === 'seek' && c.deck === 2);
	const phase = masterPhaseAtJoin(masterMs);
	assert.ok(phase > 0.5, `precondition: master past mid-beat (${phase})`);
	const expected = (FOLLOWER[100].t + phase * (FOLLOWER[101].t - FOLLOWER[100].t)) * 1000;
	assert.ok(Math.abs(seek.position_ms - expected) < 1e-6, `seek ${seek.position_ms} vs ${expected}`);
	assert.equal(m.phaseLocksForTest()[2]?.master, 1, 'locked in the same move');
});

test('Rust control: a hot cue (jump port) on a synced follower takes the same path', async () => {
	const src = readFrontendSource('src/lib/audio-engine/rust-transport.ts');
	assert.match(src, /jump: \(deck, positionMs\) => _seek\(deck, positionMs, \{ quantize: true \}\)/);
	assert.match(src, /await _join\(master, deck, \{ followerAtSec: targetMs \/ 1000, anchorOnBeat: options\.quantize \}\);/);
	// An ARMED (BeatSyncMax) jump lands an exact computed position: no re-anchoring.
	assert.match(src, /_seek\(deck, target, \{ quantize: false \}\)/);
});

test('Rust control: a paused follower, a follower with sync off, and the master still just snap', async () => {
	const masterMs = (masterAt(0.7) - m.SYNC_LEAD_SEC) * 1000;
	await playingMaster(1, MASTER, masterMs);
	m.deckStates[1].quantize_enabled = true;
	// The master: a click snaps to the beat, and with BeatSyncMax off nothing else moves.
	await m.executeInRustEngine({ type: 'seek', deck: 1, position_ms: MASTER[300].t * 1000 + 40 });
	assert.deepEqual(sent, [{ type: 'seek', deck: 1, position_ms: MASTER[300].t * 1000 }]);
	// Sync off, playing.
	sent = [];
	loadDeck(3, FOLLOWER, { quantize_enabled: true, position_ms: 30_000 });
	await m.executeInRustEngine({ type: 'play', deck: 3, playing: true });
	sent = [];
	await m.executeInRustEngine({ type: 'seek', deck: 3, position_ms: FOLLOWER[100].t * 1000 + 40 });
	assert.deepEqual(sent, [{ type: 'seek', deck: 3, position_ms: FOLLOWER[100].t * 1000 }]);
	// Sync on, paused.
	sent = [];
	loadDeck(4, FOLLOWER, { quantize_enabled: true, beat_sync_enabled: true, position_ms: 30_000 });
	await m.executeInRustEngine({ type: 'seek', deck: 4, position_ms: FOLLOWER[100].t * 1000 + 40 });
	assert.deepEqual(sent, [{ type: 'seek', deck: 4, position_ms: FOLLOWER[100].t * 1000 }]);
});

// ------------------------------------------------------- Web Audio wiring

test('SOURCE (Web Audio): only the follower re-anchor of a user seek joins on the clicked beat', () => {
	const engine = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const start = engine.indexOf('async quantizedSeek(deck: DeckId, ms: number, skipGridQuantize = false');
	assert.ok(start > 0, 'quantizedSeek not found');
	const body = engine.slice(start, engine.indexOf('\n\t}\n', start));
	const follower = body.slice(body.indexOf("if (syncPlan.kind === 'follower')"), body.indexOf("} else if (syncPlan.kind === 'master-max')"));
	assert.match(follower, /followerAnchorSec: \{ \[deck\]: targetMs \/ 1000 \}, anchorOnBeat: !skipGridQuantize,/);
	// The master's own seek (BeatSyncMax re-anchor of ITS followers) and a
	// free seek do not.
	assert.equal(body.split('anchorOnBeat').length - 1, 1, 'anchorOnBeat appears only in the follower branch');
	// The plan call honors it only together with an explicit anchor.
	assert.match(engine, /anchorOnBeat: options\.anchorOnBeat && requestedAnchorSec !== undefined/);
	// And a paused deck or one with sync off never reaches the follower branch.
	const decisions = readFrontendSource('src/lib/rb/beat-sync-decisions.ts');
	assert.match(decisions, /if \(master !== null\) return \{ kind: 'follower', master \};/);
});
