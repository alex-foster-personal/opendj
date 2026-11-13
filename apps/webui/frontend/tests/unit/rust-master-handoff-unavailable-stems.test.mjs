/**
 * Rust engine mode: unavailable stems never block a level MASTER handoff (issue #13213).
 *
 * (private path), "Why this is a wiring job": Rust mode has no
 * stems, so `_stemPowerShare` must read an unavailable stem state as the full mix.
 *
 * [if] both Rust-mode decks have unavailable stems [then] a valid level handoff still runs.
 * [if] the candidate holds the required loud level [then] MASTER moves after one beat.
 * [if] the candidate falls below threshold before hold expiry [then] MASTER stays put.
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
let handoff;
let stemGraph;
let sent = [];
const settle = () => new Promise((resolve) => setImmediate(resolve));

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/rust-mode-entry.ts');
	handoff = await loadTypeScriptModule('src/lib/rb/master-handoff.ts');
	stemGraph = await loadTypeScriptModule('src/lib/rb/stem-graph.ts');
});

let nowMs = 0;
let realNow;

beforeEach(() => {
	realNow = performance.now.bind(performance);
	nowMs = 0;
	performance.now = () => nowMs;
	sent = [];
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt: new Set(),
		send: async (command) => {
			sent.push(command);
			return { type: 'result', id: null, ok: true };
		},
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

/** Makes deck 1 the AUTO master with a fresh hold. */
function electDeck1Master() {
	m.rustMaster.deck = 1;
	m.deckStates[1].is_master = true;
	m.resetRustLevelHandoffForTest();
}

/** Kills all three EQ bands on deck 1 at `at` ms, so the master goes quiet. */
function killMasterEq(at) {
	nowMs = at;
	for (const band of ['low', 'mid', 'high']) {
		m.applyAcknowledged({ type: 'eq', deck: 1, band, value: 0 });
	}
}

/** Fires the periodic handoff check at `ms` and lets a queued election run. */
async function tickHandoff(ms) {
	nowMs = ms;
	m.maybeLevelHandoffTick(ms);
	await settle();
}

/** The candidate's heard level as the handoff rule estimates it. */
function candidateDb() {
	return handoff.heardLevelDb({ decks: m.deckStates, mixer: m.mixerState }, 2, false, new Set());
}

describe('Rust level handoff with unavailable stems (issue #13213)', () => {
	it('hands off on level when both decks have unavailable stems, with no stem commands', async () => {
		electDeck1Master();
		assert.equal(m.deckStates[1].stems.status, 'unavailable');
		assert.equal(m.deckStates[2].stems.status, 'unavailable');
		assert.ok(
			candidateDb() > handoff.CANDIDATE_LOUD_DB,
			`unavailable stems must count as the full mix, got ${candidateDb()} dB`
		);
		killMasterEq(0);
		await tickHandoff(400);
		assert.equal(m.rustMaster.deck, 1, 'handed off before one beat');
		await tickHandoff(500);
		assert.equal(m.rustMaster.deck, 2);
		assert.equal(m.rustMaster.mode, 'auto');
		assert.equal(m.deckStates[2].stems.status, 'unavailable');
		assert.deepEqual(
			sent.filter((c) => String(c.type).includes('stem')),
			[],
			'a level handoff must not need stem support in Rust mode'
		);
	});

	it('a candidate that falls below threshold before hold expiry keeps MASTER put', async () => {
		electDeck1Master();
		killMasterEq(0);
		await tickHandoff(100);
		await tickHandoff(200);
		nowMs = 300;
		m.mixerState.channels[2].fader = 0;
		m.applyAcknowledged({ type: 'fader', deck: 2, value: 0 });
		assert.ok(
			!(candidateDb() > handoff.CANDIDATE_LOUD_DB),
			`candidate still loud: ${candidateDb()} dB`
		);
		for (const t of [400, 500, 600]) {
			await tickHandoff(t);
			assert.equal(m.rustMaster.deck, 1, `handed off to a quiet candidate at ${t} ms`);
		}
		// Back up at 700 ms: the hold restarts, so one full beat must pass again.
		nowMs = 700;
		m.mixerState.channels[2].fader = 1;
		m.applyAcknowledged({ type: 'fader', deck: 2, value: 1 });
		await tickHandoff(1100);
		assert.equal(m.rustMaster.deck, 1, 'the dip must reset the hold, not resume it');
		await tickHandoff(1200);
		assert.equal(m.rustMaster.deck, 2);
	});
});
