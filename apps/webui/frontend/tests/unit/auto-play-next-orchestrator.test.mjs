// requirement: PLAY-11 (issue #3532)
// [if] AutoPlay Next aborts [then] the outgoing loop is released, [else stop].
// [if] approximateDropMs is null after bass entry [then] loop releases without waiting for position, [else stop].
// [if] the outgoing deck unloads while armed [then] the loop is released, [else stop].
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STUB_IPC = fileURLToPath(
	new URL('./fixtures/auto-play-next-stub-ipc.ts', import.meta.url)
);

let entry;

function _beats(n) {
	const out = [];
	for (let i = 0; i < n; i++) out.push({ n: (i % 4) + 1, bpm: 128, t: i * 0.469 });
	return out;
}

function _flatWaveform(length) {
	return {
		kind: 'tri',
		preview: { length: 1, low: [0], mid: [0], high: [0] },
		detail: {
			length,
			low: new Array(length).fill(0.4),
			mid: new Array(length).fill(0.3),
			high: new Array(length).fill(0.2)
		}
	};
}

function _setupLoadedDecks(harness, { incomingBassAtDetailIndex = 500 } = {}) {
	const beats = _beats(32);
	const durationMs = (beats[beats.length - 1].t + 2) * 1000;
	const outgoingWaveform = _flatWaveform(3200);
	const incomingLow = new Array(3200).fill(0.05);
	for (let i = incomingBassAtDetailIndex; i < incomingLow.length; i++) incomingLow[i] = 0.9;
	const incomingWaveform = {
		kind: 'tri',
		preview: { length: 1, low: [0], mid: [0], high: [0] },
		detail: {
			length: 3200,
			low: incomingLow,
			mid: new Array(3200).fill(0.3),
			high: new Array(3200).fill(0.2)
		}
	};
	const outgoingAnlz = { waveform: outgoingWaveform, beatgrid: { beats } };
	const incomingAnlz = { waveform: incomingWaveform, beatgrid: { beats } };
	for (const deckId of [1, 2, 3, 4]) {
		const deck = harness.deckStates[deckId];
		deck.stable_id = null;
		deck.playing = false;
		deck.is_master = false;
		deck.anlz = null;
		deck.duration_ms = null;
		deck.position_ms = 0;
		deck.loop = null;
	}
	harness.deckStates[1].stable_id = 'outgoing-track';
	harness.deckStates[1].playing = true;
	harness.deckStates[1].is_master = true;
	harness.deckStates[1].anlz = outgoingAnlz;
	harness.deckStates[1].duration_ms = durationMs;
	harness.deckStates[2].stable_id = 'incoming-track';
	harness.deckStates[2].anlz = incomingAnlz;
	harness.deckStates[2].duration_ms = durationMs;
	harness.deckStates[2].position_ms = 0;
	return { beats, durationMs };
}

before(async () => {
	entry = await loadTypeScriptModule('tests/unit/fixtures/auto-play-next-entry.ts', {
		alias: { '$lib/rb/performance-ipc.svelte': STUB_IPC }
	});
});

beforeEach(() => {
	entry.resetDispatchPerformanceCommandStub();
	entry.setDispatchPerformanceCommandStub(async () => {});
});

afterEach(async () => {
	await entry.cancelAutoPlayNext();
	entry.resetDispatchPerformanceCommandStub();
});

test('arm dispatches beat_loop on a downbeat instead of raw loop', async () => {
	const { beats } = _setupLoadedDecks(entry);
	const armed = await entry.armAutoPlayNext();
	assert.equal(armed, true);
	const beatLoop = entry
		.getDispatchPerformanceCommandCalls()
		.find((cmd) => cmd.type === 'beat_loop');
	assert.ok(beatLoop, 'expected beat_loop dispatch');
	assert.equal(beatLoop.deck, 1);
	assert.equal(beatLoop.beats, 8);
	const startIdx = beats.findIndex((beat) => beat.t * 1000 === beatLoop.start_ms);
	assert.ok(startIdx >= 0);
	assert.equal(beats[startIdx].n, 1);
	assert.ok(startIdx + 8 < beats.length);
	assert.equal(
		entry.getDispatchPerformanceCommandCalls().some((cmd) => cmd.type === 'loop' && cmd.loop !== null),
		false,
		'raw loop engage must not be used'
	);
});

test('cancelAutoPlayNext releases the outgoing loop exactly once', async () => {
	_setupLoadedDecks(entry);
	assert.equal(await entry.armAutoPlayNext(), true);
	await entry.cancelAutoPlayNext();
	const releases = entry
		.getDispatchPerformanceCommandCalls()
		.filter((cmd) => cmd.type === 'loop' && cmd.loop === null);
	assert.equal(releases.length, 1);
	assert.equal(releases[0].deck, 1);
	assert.equal(entry.autoPlayNextState.armed, false);
});

test('approximateDropMs null path releases the loop without waiting for incoming position', async () => {
	const { beats, durationMs } = _setupLoadedDecks(entry, { incomingBassAtDetailIndex: 0 });
	assert.equal(await entry.armAutoPlayNext(), true);
	entry.deckStates[2].position_ms = beats[beats.length - 2].t * 1000;
	await new Promise((resolve) => setTimeout(resolve, 300));
	const releases = entry
		.getDispatchPerformanceCommandCalls()
		.filter((cmd) => cmd.type === 'loop' && cmd.loop === null);
	assert.ok(releases.length >= 1, 'loop must release when drop cannot be approximated');
	assert.equal(entry.autoPlayNextState.armed, false);
	assert.ok(durationMs > 0);
});

test('outgoing deck unload releases the engaged loop', async () => {
	_setupLoadedDecks(entry);
	assert.equal(await entry.armAutoPlayNext(), true);
	entry.deckStates[1].stable_id = null;
	await new Promise((resolve) => setTimeout(resolve, 300));
	const releases = entry
		.getDispatchPerformanceCommandCalls()
		.filter((cmd) => cmd.type === 'loop' && cmd.loop === null);
	assert.ok(releases.length >= 1);
	assert.equal(entry.autoPlayNextState.armed, false);
});
