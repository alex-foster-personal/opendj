import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://wheel-api.example.test';
const VALID_RESPONSE = {
	schema_version: 1,
	axis: 'play_count',
	axes: [
		{ key: 'genre', label: 'Genre', enabled: true, reason: null },
		{ key: 'decade', label: 'Decade', enabled: false, reason: 'no release year data yet' },
		{ key: 'play_count', label: 'Play count', enabled: true, reason: null }
	],
	selected_axis_enabled: true,
	selected_axis_reason: null,
	total_tracks: 1,
	unclassified_track_count: 0,
	families: [
		{
			name: 'techno',
			color: '#5ec8ff',
			track_count: 1,
			genres: [
				{
					tag: 'Peak Time Techno',
					track_count: 1,
					tracks: [
						{
							stable_id: 't-1',
							title: 'One',
							artist: 'A',
							genre: 'Peak Time Techno',
							axis_value: 40,
							axis_title: '40 plays'
						}
					]
				}
			]
		}
	]
};

let wheelApi;
let originalFetch;
let requestedUrl;

before(async () => {
	wheelApi = await loadTypeScriptModule('src/routes/library-wheel/wheel-api.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

beforeEach(() => {
	requestedUrl = undefined;
	globalThis.fetch = async (input) => {
		requestedUrl = input.url;
		return new Response(JSON.stringify(VALID_RESPONSE), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
});

afterEach(() => {
	globalThis.fetch = originalFetch;
});

test('fetchLibraryWheel requests the given axis against the configured API base', async () => {
	const response = await wheelApi.fetchLibraryWheel('play_count');

	assert.equal(requestedUrl, `${API_BASE}/api/v1/library/wheel?axis=play_count`);
	assert.equal(response.total_tracks, 1);
});

test('validateLibraryWheelResponse rejects an unknown axis key', () => {
	const malformed = structuredClone(VALID_RESPONSE);
	malformed.axis = 'not-a-real-axis';

	assert.throws(
		() => wheelApi.validateLibraryWheelResponse(malformed),
		/library-wheel: axis has unknown value 'not-a-real-axis'/
	);
});

test('validateLibraryWheelResponse rejects a track missing axis_title alongside a real axis_value', () => {
	const malformed = structuredClone(VALID_RESPONSE);
	malformed.families[0].genres[0].tracks[0].axis_title = 42;

	assert.throws(
		() => wheelApi.validateLibraryWheelResponse(malformed),
		/library-wheel: families\[0\]\.genres\[0\]\.tracks\[0\]\.axis_title is not a string/
	);
});

test('validateLibraryWheelResponse accepts a disabled axis with a null axis_value', () => {
	const disabled = structuredClone(VALID_RESPONSE);
	disabled.axis = 'decade';
	disabled.selected_axis_enabled = false;
	disabled.selected_axis_reason = 'no release year data yet';
	disabled.families[0].genres[0].tracks[0].axis_value = null;
	disabled.families[0].genres[0].tracks[0].axis_title = null;

	const parsed = wheelApi.validateLibraryWheelResponse(disabled);
	assert.equal(parsed.selected_axis_enabled, false);
	assert.equal(parsed.families[0].genres[0].tracks[0].axis_value, null);
});

test('fetchLibraryWheel fails loudly on a non-OK response', async () => {
	globalThis.fetch = async () => new Response('schema missing', { status: 503 });

	await assert.rejects(
		wheelApi.fetchLibraryWheel('play_count'),
		/GET \/api\/v1\/library\/wheel failed: 503 schema missing/
	);
});

test('fetchLibraryWheel propagates validator failures without a daemon-unreachable wrap', async () => {
	globalThis.fetch = async (request) => {
		requestedUrl = request.url;
		const body = structuredClone(VALID_RESPONSE);
		body.schema_version = 2;
		return new Response(JSON.stringify(body), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	await assert.rejects(wheelApi.fetchLibraryWheel('play_count'), (error) => {
		assert.match(error.message, /library-wheel: unsupported schema_version '2'/);
		assert.equal(/daemon unreachable/.test(error.message), false);
		return true;
	});
});
