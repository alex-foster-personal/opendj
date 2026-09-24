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
 *   [if] the deferred upgrade reads one STEM_BUNDLE_HYDRATING answer as final
 *        instead of waiting through it with awaitStemArtifact [then ⛔️]
 *   [if] a held stem upgrade is not released on unload/dispose [then ⛔️]
 *   [if] a retired processor is only .disconnect()-ed, never .dispose()-ed
 *        [then ⛔️] (retired worklet + its transferred PCM leak until the whole
 *        AudioContext is torn down; disconnect() alone never releases it -
 *        see StretchDeckProcessor.dispose's docstring)
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
		'awaitStemArtifact',
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
		// awaitStemArtifact wraps probeStemArtifact and waits out a bundle the
		// server is still pulling from R2; a bare probeStemArtifact here would
		// settle a spoke's first load of every track as "no stems".
		'awaitStemArtifact',
		'fetchStemAudioArrayBuffers',
		'AlignedStemDeckProcessor.create'
	]) {
		assert.ok(deferred.includes(required), `${required} was dropped, not deferred`);
	}
	assert.ok(
		!/probeStemArtifact\(/.test(deferred),
		'_upgradeDeckStems calls probeStemArtifact directly: one STEM_BUNDLE_HYDRATING ' +
			'answer would settle the deck as unavailable while its bundle is downloading'
	);
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

test('every processor retirement path releases PCM, not just the audio graph', () => {
	const helperStart = source.indexOf('function _retireProcessor');
	assert.ok(
		helperStart > 0,
		'no _retireProcessor helper: a retirement site calling only .disconnect() ' +
			'leaves the worklet and its transferred PCM alive until the whole ' +
			'AudioContext is torn down'
	);
	const helperBody = source.slice(helperStart, source.indexOf('\n}\n', helperStart));
	assert.ok(
		helperBody.includes('.disconnect()') && helperBody.includes('.dispose()'),
		'_retireProcessor must both disconnect (synchronous silence) and dispose ' +
			'(async PCM release), per StretchDeckProcessor.dispose\'s docstring'
	);

	for (const site of [
		'function _adoptStemProcessor',
		'function _releasePendingStemUpgrade',
		'function _drainPendingStemUpgrade',
		'async function _upgradeDeckStems'
	]) {
		const start = source.indexOf(site);
		assert.ok(start > 0, `${site} not found`);
		const body = source.slice(start, source.indexOf('\n}\n', start));
		assert.ok(
			/_retireProcessor\(/.test(body),
			`${site} retires a processor without calling _retireProcessor - it will ` +
				'leak PCM on this path'
		);
		assert.ok(
			!/\bretired\.disconnect\(\)|pending\.processor\.disconnect\(\)|\bbuilt\??\.disconnect\(\)/.test(
				body
			),
			`${site} still calls .disconnect() directly on a retired processor ` +
				'instead of going through _retireProcessor'
		);
	}
});
