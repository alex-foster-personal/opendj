/**
 * Optional-resource availability store and skip-fetch gates.
 *
 * Regression lines:
 * - if remembered-absent lyrics/auto-cues/stems still issue fetch then broken
 * - if unknown caps still fetch (explicit probes keep working) then ok
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://optional-resources.example.test';
let availability;
let apiRb;
let autoCuesCache;
let originalFetch;
let fetchCalls;

before(async () => {
	availability = await loadTypeScriptModule('src/lib/rb/optional-resource-availability.ts');
	apiRb = await loadTypeScriptModule('src/lib/rb/api-rb.ts', { viteApiBase: API_BASE });
	autoCuesCache = await loadTypeScriptModule(
		'src/lib/components/rb/deck/auto-cues-cache.svelte.ts',
		{ viteApiBase: API_BASE }
	);
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

function resetFetch() {
	fetchCalls = 0;
	globalThis.fetch = async (input) => {
		fetchCalls += 1;
		const url = input instanceof Request ? input.url : String(input);
		if (url.includes('/lyrics')) {
			return Response.json(
				{ detail: 'no cached lyrics' },
				{ status: 404, headers: { 'content-type': 'application/json' } }
			);
		}
		if (url.includes('/auto-cues')) {
			return Response.json(
				{ detail: { code: 'ANALYSIS_NOT_FOUND', message: 'missing' } },
				{ status: 404, headers: { 'content-type': 'application/json' } }
			);
		}
		if (url.includes('/stems')) {
			return Response.json(
				{ detail: { code: 'STEM_BUNDLE_NOT_FOUND', message: 'missing' } },
				{ status: 404, headers: { 'content-type': 'application/json' } }
			);
		}
		return Response.json({}, { status: 200, headers: { 'content-type': 'application/json' } });
	};
}

test('remember and read optional resource caps', () => {
	availability.resetOptionalResourcesForTests();
	availability.rememberOptionalResources('sid-1', {
		lyrics: false,
		autoCues: true,
		stems: false
	});
	assert.deepEqual(availability.optionalResources('sid-1'), {
		lyrics: false,
		autoCues: true,
		stems: false,
		artwork: 'unknown'
	});
	assert.deepEqual(availability.optionalResources('sid-2'), {
		lyrics: 'unknown',
		autoCues: 'unknown',
		stems: 'unknown',
		artwork: 'unknown'
	});
});

test('remember and read artwork caps including null', () => {
	availability.resetOptionalResourcesForTests();
	availability.rememberOptionalResources('art-true', { artwork: true });
	availability.rememberOptionalResources('art-false', { artwork: false });
	availability.rememberOptionalResources('art-null', { artwork: null });
	assert.equal(availability.optionalResources('art-true').artwork, true);
	assert.equal(availability.optionalResources('art-false').artwork, false);
	assert.equal(availability.optionalResources('art-null').artwork, null);
	assert.equal(availability.optionalResources('art-missing').artwork, 'unknown');
});

test('omitting artwork does not remember null', () => {
	availability.resetOptionalResourcesForTests();
	availability.rememberOptionalResources('lyrics-only', { lyrics: false });
	assert.equal(availability.optionalResources('lyrics-only').artwork, 'unknown');
});

test('shouldFetchArtwork skips false and null, fetches unknown and true', () => {
	availability.resetOptionalResourcesForTests();
	availability.rememberOptionalResources('art-true', { artwork: true });
	availability.rememberOptionalResources('art-false', { artwork: false });
	availability.rememberOptionalResources('art-null', { artwork: null });
	assert.equal(availability.shouldFetchArtwork('art-true'), true);
	assert.equal(availability.shouldFetchArtwork('unknown-art'), true);
	assert.equal(availability.shouldFetchArtwork('art-false'), false);
	assert.equal(availability.shouldFetchArtwork('art-null'), false);
});

test('fetchTrackLyrics skips fetch when lyrics remembered absent', async () => {
	availability.resetOptionalResourcesForTests();
	resetFetch();
	availability.rememberOptionalResources('no-lyrics', { lyrics: false });
	const result = await apiRb.fetchTrackLyrics('no-lyrics');
	assert.equal(result, null);
	assert.equal(fetchCalls, 0);
});

test('fetchTrackLyrics still fetches when lyrics unknown', async () => {
	availability.resetOptionalResourcesForTests();
	resetFetch();
	const result = await apiRb.fetchTrackLyrics('unknown-lyrics');
	assert.equal(result, null);
	assert.equal(fetchCalls, 1);
});

test('probeStemArtifact skips fetch when stems remembered absent', async () => {
	availability.resetOptionalResourcesForTests();
	resetFetch();
	availability.rememberOptionalResources('no-stems', { stems: false });
	const probe = await apiRb.probeStemArtifact('no-stems');
	assert.deepEqual(probe, { status: 'unavailable', error: 'no stem bundle advertised' });
	assert.equal(fetchCalls, 0);
});

test('probeStemArtifact still fetches when stems unknown', async () => {
	availability.resetOptionalResourcesForTests();
	resetFetch();
	const probe = await apiRb.probeStemArtifact('unknown-stems');
	assert.equal(probe.status, 'unavailable');
	assert.equal(fetchCalls, 1);
});

test('ensureAutoCues skips fetch when auto-cues remembered absent', () => {
	availability.resetOptionalResourcesForTests();
	resetFetch();
	availability.rememberOptionalResources('no-cues', { autoCues: false });
	autoCuesCache.ensureAutoCues('no-cues');
	const entry = autoCuesCache.getAutoCuesEntry('no-cues');
	assert.deepEqual(entry, { status: 'error', code: 'ANALYSIS_NOT_FOUND' });
	assert.equal(fetchCalls, 0);
});

test('ensureAutoCues still fetches when auto-cues unknown', async () => {
	availability.resetOptionalResourcesForTests();
	resetFetch();
	autoCuesCache.ensureAutoCues('unknown-cues');
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(fetchCalls, 1);
	const entry = autoCuesCache.getAutoCuesEntry('unknown-cues');
	assert.equal(entry?.status, 'error');
	assert.equal(entry?.code, 'ANALYSIS_NOT_FOUND');
});
