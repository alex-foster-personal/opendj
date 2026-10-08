import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://analytics-api.example.test';
const VALID_RESPONSE = {
	schema_version: 1,
	filters: { share_state: null, limit: 50 },
	summary: { sessions: 2, plays: 4, unique_tracks: 3, completed_duration_s: 5400 },
	sessions: [
		{
			session_id: 'session-1',
			started_at: '2026-07-21T22:00:00+00:00',
			ended_at: null,
			duration_s: null,
			share_state: 'private',
			play_count: 2,
			unique_track_count: 2
		}
	],
	top_tracks: [
		{
			stable_id: 'track-a',
			title: 'Alpha',
			artist: 'Artist One',
			play_count: 2,
			last_played_at: '2026-07-21T22:00:05+00:00'
		}
	]
};

let analyticsApi;
let originalFetch;
let requestedUrl;

before(async () => {
	analyticsApi = await loadTypeScriptModule('src/routes/play-analytics/analytics-api.ts', {
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

test('fetchPlayAnalytics uses configured API base and serialized HTTP filters', async () => {
	const response = await analyticsApi.fetchPlayAnalytics('shared_local', 25);

	assert.equal(
		requestedUrl,
		`${API_BASE}/api/play-analytics?share_state=shared_local&limit=25`
	);
	assert.equal(response.summary.plays, 4);
});

test('validatePlayAnalyticsResponse rejects malformed summary values', () => {
	const malformed = structuredClone(VALID_RESPONSE);
	malformed.summary.plays = 'four';

	assert.throws(
		() => analyticsApi.validatePlayAnalyticsResponse(malformed),
		/play-analytics: summary.plays is not a finite number/
	);
});
test('fetchPlayAnalytics fails loudly on a non-OK response', async () => {
	globalThis.fetch = async () => new Response('schema missing', { status: 503 });

	await assert.rejects(
		analyticsApi.fetchPlayAnalytics(null, 50),
		/GET \/api\/play-analytics failed: 503 schema missing/
	);
});

test('fetchPlayAnalytics propagates validator failures without daemon-unreachable wrap', async () => {
	// openapi-fetch calls fetch(request) with one Request object.
	globalThis.fetch = async (request) => {
		requestedUrl = request.url;
		const body = structuredClone(VALID_RESPONSE);
		body.schema_version = 2;
		return new Response(JSON.stringify(body), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	await assert.rejects(analyticsApi.fetchPlayAnalytics(null, 50), (error) => {
		assert.match(error.message, /play-analytics: unsupported schema_version '2'/);
		assert.equal(/daemon unreachable/.test(error.message), false);
		return true;
	});
});

// The 503 bodies below are the real route's payloads, captured from
// GET /api/play-analytics on a fresh library (Wed 7 Oct 2026 public-launch
// sweep) and from a store missing its table. The server half is held to the
// same prefix by tests/play_analytics/test_surfaces.py, and the page half runs
// against the real engine in tests/e2e/play-analytics-empty.spec.ts.
const MISSING_STORE_BODY = JSON.stringify({
	detail: 'event store does not exist: /Users/dev/data/sets/sets.db'
});
const MISSING_TABLE_BODY = JSON.stringify({ detail: "required table 'sets' is missing" });

test('a missing event store (no sets recorded yet) is recognized from the real 503 body', () => {
	assert.equal(analyticsApi.isNoEventStore(503, MISSING_STORE_BODY), true);
	assert.ok(MISSING_STORE_BODY.includes(analyticsApi.NO_EVENT_STORE_DETAIL));
});

test('control: a broken event store or another status is never read as "no sets yet"', () => {
	assert.equal(analyticsApi.isNoEventStore(503, MISSING_TABLE_BODY), false);
	assert.equal(analyticsApi.isNoEventStore(500, MISSING_STORE_BODY), false);
	assert.equal(analyticsApi.isNoEventStore(503, 'not json'), false);
});
