import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

// c256bca1 - "expose load and memory KPIs" landed with no test at all.
// grep for lastDeckLoadEvents / __mdtLastLoads / last_load_stages /
// deckPcmEstimatedBytes across tests/unit returned nothing before this file.
//
// These are the numbers an agent or the CLI reads back to decide whether a load
// regressed, so a silent break shows up as "the KPI is always empty" rather
// than as a crash - the failure mode no other test would catch.
//
// Regression lines:
// - if lastDeckLoadEvents stops filtering on the deck-load prefix then unrelated
//   perf rows pollute the load KPI
// - if it stops returning newest-first then "the last load" reports the oldest
// - if the default limit is not one row per deck then a 4-deck load drops rows
// - if the stage map is shared rather than copied then a caller mutating the KPI
//   corrupts the ring log / engine state
// - if installPerfEventLogGlobal stops installing __mdtLastLoads then the
//   documented DevTools KPI helper is gone
// - if the IPC deck snapshot drops last_load_stages then agent consumers lose
//   the per-stage breakdown
// - if decoded-memory accounting stops counting stems then a stems-ready deck
//   under-reports its PCM footprint by 4x

// The decoded-memory guard below reads the engine as source text, through
// engine-source.mjs, which tracks a LIST of engine source files. T4 splits
// audio-engine.svelte.ts into player/* behind a barrel; a guard hardcoding the
// old path would then slice an empty body out of a -1 index and assert nothing.
let perfLog;
// One bundle so `deckStates` is the exact object queryPerformanceState reads.
// Loading the two modules separately gives two unrelated copies of the state,
// which makes any snapshot assertion pass no matter what the code does.
let perfIpc;

before(async () => {
	perfLog = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts');
	perfIpc = await loadTypeScriptModule('tests/unit/fixtures/perf-ipc-entry.ts');
});

test('lastDeckLoadEvents returns deck-load rows newest first', () => {
	perfLog.recordPerfTiming('deck-load', { decode: 10, total: 11 }, 1);
	perfLog.recordPerfTiming('deck-load', { decode: 20, total: 21 }, 2);
	perfLog.recordPerfTiming('deck-load', { decode: 30, total: 31 }, 3);

	const rows = perfLog.lastDeckLoadEvents(3);
	assert.deepEqual(
		rows.map((r) => r.deck),
		[3, 2, 1],
		'if the newest load is not first then "the last load" names the wrong one'
	);
	assert.equal(rows[0].stages.decode, 30, 'the stage map must ride along with the row');
});

test('lastDeckLoadEvents ignores perf rows that are not deck loads', () => {
	perfLog.recordPerfTiming('deck-load', { total: 41 }, 4);
	perfLog.recordPerfEvent('sync-failure', 'could not phase lock', 2);
	perfLog.recordPerfTiming('artwork-decode', { total: 5 }, 2);

	const rows = perfLog.lastDeckLoadEvents(1);
	assert.equal(rows.length, 1);
	assert.equal(
		rows[0].kind,
		'deck-load',
		'if non-load rows leak in then the load KPI reports unrelated timings'
	);
	assert.equal(rows[0].deck, 4);
});

test('lastDeckLoadEvents matches prefixed deck-load kinds', () => {
	perfLog.recordPerfTiming('deck-load-stems', { fetch: 7, total: 9 }, 1);
	const rows = perfLog.lastDeckLoadEvents(1);
	assert.equal(
		rows[0].kind,
		'deck-load-stems',
		'if the match is exact rather than a prefix then stem loads vanish from the KPI'
	);
});

test('lastDeckLoadEvents honours its limit and defaults to one row per deck', () => {
	for (const deck of [1, 2, 3, 4]) {
		perfLog.recordPerfTiming('deck-load', { total: deck }, deck);
	}
	assert.equal(perfLog.lastDeckLoadEvents(2).length, 2, 'an explicit limit must be respected');
	assert.equal(
		perfLog.lastDeckLoadEvents().length,
		4,
		'if the default is not 4 then a full 4-deck load cannot be read back in one call'
	);
	assert.deepEqual(
		perfLog.lastDeckLoadEvents().map((r) => r.deck),
		[4, 3, 2, 1]
	);
});

test('installPerfEventLogGlobal installs both DevTools KPI helpers', () => {
	const priorWindow = globalThis.window;
	const w = {};
	globalThis.window = w;
	try {
		perfLog.installPerfEventLogGlobal();
		assert.equal(typeof w.__mdtPerfLog, 'function', '__mdtPerfLog must stay installed');
		assert.equal(
			typeof w.__mdtLastLoads,
			'function',
			'if __mdtLastLoads is missing then the documented load KPI helper is gone'
		);
		perfLog.recordPerfTiming('deck-load', { total: 99 }, 2);
		assert.equal(w.__mdtLastLoads(1)[0].stages.total, 99);
		assert.equal(
			w.__mdtLastLoads().length,
			4,
			'the global must carry the same 4-row default as the module function'
		);
	} finally {
		if (priorWindow === undefined) delete globalThis.window;
		else globalThis.window = priorWindow;
	}
});

test('the IPC deck snapshot exposes load latency and a defensive copy of the stages', () => {
	const deck = perfIpc.deckStates[1];
	const priorLatency = deck.last_load_latency_ms;
	const priorStages = deck.last_load_stages;
	try {
		deck.last_load_latency_ms = 1234;
		deck.last_load_stages = { fetch: 200, decode: 900, graph: 134 };

		const snapshot = perfIpc.queryPerformanceState().decks[1];
		assert.equal(snapshot.last_load_latency_ms, 1234, 'the load KPI must reach IPC consumers');
		assert.deepEqual(snapshot.last_load_stages, { fetch: 200, decode: 900, graph: 134 });

		snapshot.last_load_stages.decode = -1;
		assert.equal(
			deck.last_load_stages.decode,
			900,
			'if the stage map is shared rather than copied then an IPC consumer ' +
				'mutating the snapshot corrupts live engine state'
		);
	} finally {
		deck.last_load_latency_ms = priorLatency;
		deck.last_load_stages = priorStages;
	}
});

test('the IPC deck snapshot exposes the durable successful-load generation', () => {
	const deck = perfIpc.deckStates[1];
	const priorGeneration = deck.load_generation;
	try {
		deck.load_generation = 14;
		assert.equal(
			perfIpc.queryPerformanceState().decks[1].load_generation,
			14,
			'IPC consumers must be able to observe a completed reload after its blank state has passed'
		);
	} finally {
		deck.load_generation = priorGeneration;
	}
});

test('the IPC deck snapshot reports null stages before any load', () => {
	const deck = perfIpc.deckStates[3];
	const priorStages = deck.last_load_stages;
	try {
		deck.last_load_stages = null;
		assert.equal(
			perfIpc.queryPerformanceState().decks[3].last_load_stages,
			null,
			'if null becomes {} then a consumer cannot tell "never loaded" from "no stages"'
		);
	} finally {
		deck.last_load_stages = priorStages;
	}
});

test('decoded-memory accounting charges a stems-ready deck for its four stem buffers', () => {
	// The arithmetic itself lives in estimateDeckPcmBytes (deck-audio-snapshot.ts,
	// listed in ENGINE_SOURCE_PATHS) since deckPcmEstimatedBytes in the engine is
	// now a thin wrapper that reads _rt[deck].audioBuffer / deckStates[deck].stems
	// and hands them to it. Brace-matched off the declaration, so the body cannot
	// silently widen to the rest of the file (or narrow to '') when a neighbouring
	// symbol moves.
	const body = engineBlockAfter(
		'export function estimateDeckPcmBytes(decks: Iterable<DeckPcmEstimateInput>): number {'
	);
	// The wrapper's only real behaviour is reading _rt[deck].audioBuffer, which
	// only exists behind a real decoded AudioBuffer, so the arithmetic itself is
	// asserted here rather than faked with a stub buffer. Recorded as an audit
	// finding for an e2e KPI check.
	const wrapperBody = engineBlockAfter('export function deckPcmEstimatedBytes(): number {');
	assert.match(
		wrapperBody,
		/audioBuffer: _rt\[deck\]\.audioBuffer,/,
		'if the wrapper stops reading the real decoded buffer then the KPI is fed nothing'
	);
	assert.match(
		wrapperBody,
		/stemsReady: deckStates\[deck\]\.stems\.status === 'ready'/,
		'if the wrapper stops reading the real stems-ready state then a stems-ready ' +
			'deck is never charged the 4x term'
	);
	assert.match(
		body,
		/const mixBytes = buffer\.length \* buffer\.numberOfChannels \* 4;/,
		'if the per-sample width stops being 4 bytes then the Float32 estimate is wrong'
	);
	assert.match(
		body,
		/if \(deck\.stemsReady\) total \+= mixBytes \* 4;/,
		'if the stems term is dropped or the multiplier changes then a stems-ready ' +
			'deck under-reports its decoded footprint'
	);
});
