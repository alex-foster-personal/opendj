/**
 * Rust engine mode: level-based MASTER handoff (issue #7558).
 *
 * [if] EQ kill on the Rust master does not hand off after one beat [then] broken.
 * [if] a THRU master loses MASTER when crossfader parks on the other side [then] broken.
 * [if] a faded master does not hand off when the other deck starts playing [then] broken.
 * [if] a LOCKED master moves on level [then] broken.
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, describe, it } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
let handoff;
let stemGraph;
const settle = () => new Promise((resolve) => setImmediate(resolve));

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/rust-mode-entry.ts');
	handoff = await loadTypeScriptModule('src/lib/rb/master-handoff.ts');
	stemGraph = await loadTypeScriptModule('src/lib/rb/stem-graph.ts');
});

let nowMs = 0;
let realNow;
let frameNo = 10_000;

beforeEach(() => {
	realNow = performance.now.bind(performance);
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

function electDeck1Master() {
	m.rustMaster.deck = 1;
	m.deckStates[1].is_master = true;
	m.resetRustLevelHandoffForTest();
}

async function tickHandoff(ms) {
	nowMs = ms;
	m.maybeLevelHandoffTick(ms);
	await settle();
}

function stateFrame({ d1Playing = true, d2Playing = true } = {}) {
	frameNo += 1;
	return {
		type: 'state',
		frame: frameNo,
		sample_rate: 48000,
		engine_time_ns: 0,
		decks: [
			{
				deck: 1,
				loaded: true,
				playing: d1Playing,
				position_ms: 0,
				duration_ms: 300_000,
				rate: 1,
				tempo: 1,
				cue_ms: null,
				loop: null
			},
			{
				deck: 2,
				loaded: true,
				playing: d2Playing,
				position_ms: 0,
				duration_ms: 300_000,
				rate: 1,
				tempo: 1,
				cue_ms: null,
				loop: null
			}
		],
		mixer: { crossfader: m.mixerState.crossfader, master_volume: 1 },
		master: { muted: false, rms: 0 }
	};
}

describe('Rust transport level handoff (issue #7558)', () => {
	it('EQ kill: hands off after one beat, not before', async () => {
		electDeck1Master();
		nowMs = 0;
		for (const band of ['low', 'mid', 'high']) {
			m.applyAcknowledged({ type: 'eq', deck: 1, band, value: 0 });
		}
		await tickHandoff(400);
		assert.equal(m.rustMaster.deck, 1);
		await tickHandoff(500);
		assert.equal(m.rustMaster.deck, 2);
		const events = m.readPerfEvents().filter((e) => e.kind === 'master-handoff');
		assert.ok(events.some((e) => e.message.includes('master -inf dB')), events.map((e) => e.message).join('; '));
	});

	it('THRU: master on THRU stays when crossfader is on the other deck', async () => {
		electDeck1Master();
		m.mixerState.channels[1].assign = 'THRU';
		nowMs = 0;
		m.applyAcknowledged({ type: 'assign', deck: 1, assign: 'THRU' });
		m.applyAcknowledged({ type: 'crossfader', value: 1 });
		await tickHandoff(3000);
		assert.equal(m.rustMaster.deck, 1);
	});

	it('THRU: a THRU candidate takes over from a crossfaded-out master', async () => {
		electDeck1Master();
		m.applyAcknowledged({ type: 'assign', deck: 2, assign: 'THRU' });
		m.applyAcknowledged({ type: 'crossfader', value: 1 });
		await tickHandoff(3000);
		assert.equal(m.rustMaster.deck, 2);
	});

	it('play after fade: hands off one beat after the other deck starts playing', async () => {
		electDeck1Master();
		m.deckStates[2].playing = false;
		m.deckStates[2].audible = false;
		nowMs = 0;
		m.applyAcknowledged({ type: 'fader', deck: 1, value: 0.01 });
		await tickHandoff(2000);
		assert.equal(m.rustMaster.deck, 1);
		m.mirrorEngineState(stateFrame({ d1Playing: true, d2Playing: true }));
		await tickHandoff(2500);
		assert.equal(m.rustMaster.deck, 2);
	});

	it('a LOCKED master never moves on level', async () => {
		electDeck1Master();
		m.rustMaster.mode = 'locked';
		nowMs = 0;
		for (const band of ['low', 'mid', 'high']) {
			m.applyAcknowledged({ type: 'eq', deck: 1, band, value: 0 });
		}
		await tickHandoff(5000);
		assert.equal(m.rustMaster.deck, 1);
		assert.equal(m.rustMaster.mode, 'locked');
	});

	it('applyAcknowledged with no client does not throw on mixer acks', () => {
		m.installRustEngineClientForTest(null);
		assert.doesNotThrow(() => m.applyAcknowledged({ type: 'fader', deck: 1, value: 0 }));
	});
});

describe('Rust level handoff wiring (source guards)', () => {
	const transport = () => readFrontendSource('src/lib/audio-engine/rust-transport.ts');
	const engine = () => readFrontendSource('src/lib/audio-engine/rust-engine.ts');

	it('rust-engine calls maybeLevelHandoff on mixer acks and maybeLevelHandoffTick on the state feed', () => {
		const eng = engine();
		assert.match(eng, /maybeLevelHandoff\(\)/);
		assert.match(eng, /maybeLevelHandoffTick\(\)/);
	});

	it('rust-transport implements target-aware level election', () => {
		const src = transport();
		assert.match(src, /_levelHandoff = LEVEL_HANDOFF_IDLE;/);
		assert.match(src, /to\?: DeckId/);
		assert.match(src, /stepLiveLevelHandoff\(/);
		assert.match(src, /runAutomaticMasterElection\(/);
		assert.match(src, /link\.lastState\?\.master\.muted/);
		assert.match(src, /extrouteQuery: '' as const/);
		assert.match(src, /if \(link\.client === null\) return;/);
		assert.match(src, /function _setDeckMaster\([\s\S]*_levelHandoffGeneration \+= 1;/);
	});
});
