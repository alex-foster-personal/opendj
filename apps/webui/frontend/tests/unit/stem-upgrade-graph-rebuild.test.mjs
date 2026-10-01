// requirement: an audio-graph rebuild in the middle of a stem download must
// not lose the stems (STEM-37).
//
// Output-stall recovery (issue #2155) swaps in a new AudioContext and re-attaches
// the decks. A deck whose stems were still downloading (status 'loading', which
// STEM-37 can now hold for minutes while R2 hydrates) used to get no new upgrade,
// and its old task then decoded on the disposed context and flipped the deck to
// 'error'.
//
// [if] a deck's stems are loading when the graph is rebuilt [then] its snapshot
//   says so, and the rebuild restarts the upgrade on the new context
// [if] the stems were settled unavailable [then] no upgrade restarts (control:
//   a track with no bundle must not start polling again after every rebuild)
// [if] the context an upgrade started on is no longer the engine's [then] that
//   task counts as stale and returns silently instead of reporting an error
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let graph;
let engine;

before(async () => {
	graph = await loadTypeScriptModule('src/lib/rb/deck-channel-graph.ts');
	engine = readFileSync(
		new URL('../../src/lib/rb/audio-engine.svelte.ts', import.meta.url),
		'utf8'
	);
});

function snap(status) {
	const rt = {
		audioBuffer: { length: 1, sampleRate: 44_100, duration: 1 },
		desiredActive: false,
		controlTempoRatio: 1,
		controlMasterTempoEnabled: false,
		controlKeyShiftSemitones: 0,
		controlLoop: null
	};
	const st = { position_ms: 0, stable_id: 'sid-1', playing: false, stems: { status } };
	return graph.snapLoadedDeck(1, rt, st);
}

test('a deck mid-download is snapshotted as loading', () => {
	const loading = snap('loading');
	assert.equal(loading.stemsLoading, true);
	assert.equal(loading.stemsReady, false);
});

test('control: settled decks are not snapshotted as loading', () => {
	for (const status of ['ready', 'unavailable', 'error']) {
		assert.equal(snap(status).stemsLoading, false, status);
	}
	assert.equal(snap('ready').stemsReady, true);
});

test('the rebuild restarts loading and ready decks, and only those', () => {
	const start = engine.indexOf('maybeUpgradeStems: (snap, buffer, ctx) => {');
	assert.ok(start > 0, 'maybeUpgradeStems binding not found in the engine');
	const body = engine.slice(start, engine.indexOf('}', engine.indexOf('{', start + 40)));
	assert.match(body, /snap\.stemsReady \|\| snap\.stemsLoading/);
	assert.match(body, /_upgradeDeckStems\(snap\.deck, snap\.stableId, _rt\[snap\.deck\]\.loadToken, ctx, buffer\)/);
});

test('an upgrade started on a replaced context is stale', () => {
	const start = engine.indexOf('async function _upgradeDeckStems(');
	assert.ok(start > 0, '_upgradeDeckStems not found in the engine');
	const body = engine.slice(start, engine.indexOf('let built', start));
	assert.match(body, /const stale = \(\): boolean => token !== rt\.loadToken \|\| ctx !== _ctx \|\| _stemDecodeBlocked\(\);/);
});
