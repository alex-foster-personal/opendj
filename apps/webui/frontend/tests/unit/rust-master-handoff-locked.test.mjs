/**
 * Rust engine mode: the DJ lock against level-based MASTER handoff (issue #13212,
 * spec 70ef4458_rust-level-handoff.md "Scope").
 *
 * [if] a locked master becomes quiet and a follower becomes loud [then] MASTER stays put [⛔️ if it moves on level].
 * [if] the master is unlocked under the same levels [then] MASTER moves on level after one beat.
 * [if] playback ends on the locked master [then] the forced natural-end handoff still moves MASTER and drops the lock.
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
let stemGraph;
const settle = () => new Promise((resolve) => setImmediate(resolve));

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/rust-mode-entry.ts');
	stemGraph = await loadTypeScriptModule('src/lib/rb/stem-graph.ts');
});

let nowMs = 0;
let realNow;
let frameNo = 20_000;

beforeEach(() => {
	realNow ??= performance.now.bind(performance);
	nowMs = 0;
	performance.now = () => nowMs;
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt: new Set(),
		send: async () => ({ type: 'result', id: null, ok: true }),
		positionMs: () => null
	});
	m.link.toast = () => {};
	m.resetRustLevelHandoffForTest();
	m.resetPerfEventLog();
	m.rustMaster.deck = null;
	m.rustMaster.mode = 'auto';
	for (const d of [1, 2, 3, 4]) {
		const st = m.deckStates[d];
		st.stable_id = d <= 2 ? `track-${d}` : null;
		st.playing = d <= 2;
		st.audible = d <= 2;
		st.bpm = 120;
		st.pitch = 1;
		st.beat_sync_enabled = false;
		st.is_master = false;
		st.stems = stemGraph.unavailableStemDeckState();
		const ch = m.mixerState.channels[d];
		ch.trim = 0.5;
		ch.fader = 1;
		ch.eq_low = 0.5;
		ch.eq_mid = 0.5;
		ch.eq_high = 0.5;
		ch.filter = 0.5;
		ch.assign = d === 1 ? 'A' : d === 2 ? 'B' : 'A';
	}
	m.mixerState.crossfader = 0.5;
	m.mixerState.master = 1;
});

after(() => {
	if (realNow !== undefined) performance.now = realNow;
	m.installRustEngineClientForTest(null);
});

/** Make deck 1 the master in `mode` with a fresh level-handoff hold. */
function electDeck1Master(mode) {
	m.rustMaster.deck = 1;
	m.rustMaster.mode = mode;
	m.deckStates[1].is_master = true;
	m.resetRustLevelHandoffForTest();
}

/** Kill every EQ band on deck 1 through acks so the master goes quiet while deck 2 stays loud. */
function quietMasterLoudFollower() {
	for (const band of ['low', 'mid', 'high']) {
		m.applyAcknowledged({ type: 'eq', deck: 1, band, value: 0 });
	}
}

/** Advance the stubbed clock, fire the throttled level tick, and let a queued election run. */
async function tickHandoff(ms) {
	nowMs = ms;
	m.maybeLevelHandoffTick(ms);
	await settle();
}

/** The master-handoff perf event messages written so far. */
function handoffMessages() {
	return m
		.readPerfEvents()
		.filter((e) => e.kind === 'master-handoff')
		.map((e) => e.message);
}

/** One engine state frame for decks 1 and 2 with the given play flags. */
function stateFrame({ d1Playing, d2Playing }) {
	frameNo += 1;
	const deck = (n, playing) => ({
		deck: n,
		loaded: true,
		playing,
		position_ms: playing ? 0 : 300_000,
		duration_ms: 300_000,
		rate: 1,
		tempo: 1,
		cue_ms: null,
		loop: null,
		wave_gains: { low: 1, mid: 1, high: 1 }
	});
	return {
		type: 'state',
		frame: frameNo,
		sample_rate: 48000,
		engine_time_ns: 0,
		decks: [deck(1, d1Playing), deck(2, d2Playing)],
		mixer: { crossfader: m.mixerState.crossfader, master_volume: 1 },
		master: { muted: false, rms: 0 }
	};
}

describe('Rust level handoff respects the DJ lock (issue #13212)', () => {
	it('a locked quiet master keeps MASTER while a louder follower plays', async () => {
		// The lock, not the levels, is what stops a level handoff: many beats of quiet still do nothing.
		electDeck1Master('locked');
		quietMasterLoudFollower();
		for (const ms of [500, 1000, 2500, 5000]) await tickHandoff(ms);
		assert.equal(m.rustMaster.deck, 1, 'if a locked master moves on level then broken');
		assert.equal(m.rustMaster.mode, 'locked', 'if the level path drops the lock then broken');
		assert.equal(m.deckStates[1].is_master, true);
		assert.equal(m.deckStates[2].is_master, false);
		assert.deepEqual(
			handoffMessages(),
			[],
			'if a locked master writes a handoff event then broken'
		);
	});

	it('the same levels with the master unlocked hand MASTER off after one beat', async () => {
		// Control for the lock case: identical mixer state, AUTO mode, so the level rule fires.
		electDeck1Master('auto');
		quietMasterLoudFollower();
		await tickHandoff(400);
		assert.equal(m.rustMaster.deck, 1, 'if MASTER moves before one beat of quiet then broken');
		await tickHandoff(500);
		assert.equal(m.rustMaster.deck, 2, 'if an unlocked quiet master keeps MASTER then broken');
		assert.equal(m.rustMaster.mode, 'auto');
		const messages = handoffMessages();
		assert.equal(messages.length, 1, JSON.stringify(messages));
		assert.match(messages[0], /^master 1 -> 2 \(master-left\): master -inf dB, deck 2 /);
	});

	it('playback ending on a locked master still hands off, as a forced natural-end election', async () => {
		// Forced rule, not the level rule: the lock holds through the quiet, then the play-out moves it.
		electDeck1Master('locked');
		quietMasterLoudFollower();
		await tickHandoff(5000);
		assert.equal(
			m.rustMaster.deck,
			1,
			'if the lock does not hold before the track ends then broken'
		);
		nowMs = 5100;
		m.mirrorEngineState(stateFrame({ d1Playing: false, d2Playing: true }));
		await settle();
		await settle();
		assert.equal(m.deckStates[1].playing, false);
		assert.equal(
			m.rustMaster.deck,
			2,
			'if a locked master that played out keeps MASTER then broken'
		);
		assert.equal(m.rustMaster.mode, 'auto', 'if the lock outlives the play-out then broken');
		assert.deepEqual(
			handoffMessages(),
			['master 1 -> 2 (natural-end)'],
			'if the play-out handoff is reported as a level handoff (or not at all) then broken'
		);
	});
});
