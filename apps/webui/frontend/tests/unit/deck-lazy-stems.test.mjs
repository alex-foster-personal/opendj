/**
 * LAZY-STEMS contract: the deck's critical path must not carry stem work, and
 * "still loading" must stay distinguishable from "no bundle exists".
 *
 * These are STRUCTURAL assertions over the engine source rather than a live
 * load, because a real load needs Web Audio. The behavioural half lives in
 * tests/e2e/webkit-deckload.spec.ts, which drives a real engine in WebKit and
 * reads the stage map the load transaction actually wrote.
 *
 * MUTATION CHECK (run before trusting any of this): revert
 * apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts to the eager-stem
 * version and every test here must FAIL. Measured on the change that added it:
 * reverted -> 4 failing, restored -> 4 passing.
 *
 *   [if] probeStem is back in the load() fetch group [then ⛔️]
 *   [if] stem decode is back in the pre-swap section [then ⛔️]
 *   [if] 'loading' collapses back into 'unavailable' [then ⛔️]
 *   [if] a held stem upgrade is not released on unload/dispose [then ⛔️]
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';
import { readFileSync } from 'node:fs';

import { loadTypeScriptModule } from './load-typescript.mjs';

const ENGINE = 'src/lib/rb/audio-engine.svelte.ts';
let source;
let stems;

before(async () => {
	source = readFileSync(new URL(`../../${ENGINE}`, import.meta.url), 'utf8');
	stems = await loadTypeScriptModule('src/lib/rb/stem-graph.ts');
});

/** The body of `async load(...)` up to the point the deck is published, which
 * is everything a DJ waits through before the deck can play. */
function criticalPath() {
	const start = source.indexOf('async load(deck: DeckId, stable_id: string)');
	assert.ok(start > 0, 'load() not found - this test is reading the wrong file');
	const end = source.indexOf('stages.totalBeforeSwap', start);
	assert.ok(end > start, 'totalBeforeSwap marker not found inside load()');
	return source.slice(start, end);
}

test('no stem fetch, decode or processor build sits in the deck critical path', () => {
	const path = criticalPath();
	for (const banned of [
		'probeStemArtifact',
		'fetchStemAudioArrayBuffers',
		'AlignedStemDeckProcessor.create',
		"time('probeStem'",
		"time('fetchStems'",
		"time('decodeStems'",
		"time('stemProcessorCreate'"
	]) {
		assert.ok(
			!path.includes(banned),
			`${banned} is back in the deck critical path; that is the regression this ` +
				'change removed (a DJ waits through it before the deck can play)'
		);
	}
});

test('the deferred upgrade owns every stage the critical path gave up', () => {
	const start = source.indexOf('async function _upgradeDeckStems');
	assert.ok(start > 0, '_upgradeDeckStems not found');
	const deferred = source.slice(start, source.indexOf('\n}\n', start));
	for (const required of [
		'probeStemArtifact',
		'fetchStemAudioArrayBuffers',
		'AlignedStemDeckProcessor.create'
	]) {
		assert.ok(deferred.includes(required), `${required} was dropped, not deferred`);
	}
	// Never awaited by load(): awaiting it would put the whole cost straight
	// back on the critical path while looking like it had moved.
	assert.ok(
		/void _upgradeDeckStems\(/.test(source),
		'_upgradeDeckStems must be kicked with `void`, never awaited by load()'
	);
	assert.ok(
		!/await _upgradeDeckStems\(/.test(source),
		'_upgradeDeckStems is awaited somewhere - the deferral is undone'
	);
});

test('loading is a distinct settled-ness from unavailable', () => {
	const loading = stems.loadingStemDeckState();
	const unavailable = stems.unavailableStemDeckState();

	assert.equal(loading.status, 'loading');
	assert.equal(unavailable.status, 'unavailable');
	assert.notEqual(
		loading.status,
		unavailable.status,
		'collapsing these lets the UI claim a track has no stems while its bundle ' +
			'is still downloading'
	);
	// No capability may be advertised before the processor exists.
	assert.deepEqual(loading.available_controls, []);
	assert.equal(loading.alignment, null);
	assert.equal(loading.layout, null);
	// `loading` is not an error state, so it must carry no error text.
	assert.equal(loading.error, null);
});

test('a held stem upgrade is released on every path that abandons it', () => {
	assert.ok(
		source.includes('function _releasePendingStemUpgrade'),
		'no release helper: a prepared-but-never-landed processor would leak worklet nodes'
	);
	const calls = source.match(/_releasePendingStemUpgrade\(/g) ?? [];
	// definition + unload + dispose + track swap in load()
	assert.ok(
		calls.length >= 4,
		`expected the release on unload, dispose and track swap; found ${calls.length} references`
	);
	assert.ok(
		source.includes('_drainPendingStemUpgrade(deck)'),
		'nothing lands a bundle that finished while the deck was playing'
	);
});
