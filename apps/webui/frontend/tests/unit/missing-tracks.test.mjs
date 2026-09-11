import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://missing-tracks.example.test';

let missing;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

function brokenTrack(overrides = {}) {
	return {
		stable_id: 'sid-broken-1',
		title: 'Gone File',
		artist: 'Missing Artist',
		album: null,
		basename: 'gone.mp3',
		parent_dir: '/old',
		original_path: '/old/gone.mp3',
		key: '8A',
		bpm: 128,
		rating: 3,
		duration_ms: 180000,
		file_exists: true,
		is_streaming: true,
		playlist_ids: [],
		vendor_id: '42',
		...overrides
	};
}

before(async () => {
	missing = await loadTypeScriptModule(
		'src/lib/components/rb/browser/missing-tracks.ts',
		{ viteApiBase: API_BASE }
	);
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('isMissingTracksId matches only the reserved sentinel', () => {
	assert.equal(missing.isMissingTracksId('missing'), true);
	assert.equal(missing.isMissingTracksId('all'), false);
	assert.equal(missing.isMissingTracksId('pl-1'), false);
	assert.equal(missing.isMissingTracksId(null), false);
	assert.equal(missing.MISSING_TRACKS_ID, 'missing');
	assert.equal(missing.MISSING_TRACKS_NAME, 'Missing Tracks');
});

test('missingTracksNode uses the reserved id and missing_tracks kind', () => {
	const node = missing.missingTracksNode(7);
	assert.equal(node.playlist_id, 'missing');
	assert.equal(node.name, 'Missing Tracks');
	assert.equal(node.kind, 'missing_tracks');
	assert.equal(node.track_count, 7);
	assert.equal(node.broken_count, 7);
	assert.deepEqual(node.children, []);
});

test('brokenTrackToBrowserRow copies listing fields and forces unavailable disk state', () => {
	const row = missing.brokenTrackToBrowserRow(brokenTrack(), 1);
	assert.equal(row.stable_id, 'sid-broken-1');
	assert.equal(row.title, 'Gone File');
	assert.equal(row.artist, 'Missing Artist');
	assert.equal(row.key, '8A');
	assert.equal(row.bpm, 128);
	assert.equal(row.rating, 3);
	assert.equal(row.duration_ms, 180000);
	assert.equal(row.order, 1);
	assert.equal(row.file_exists, false);
	assert.equal(row.is_streaming, false);
	assert.equal(row.energy, null);
	assert.equal(row.energy_source, null);
	assert.equal(row.energy_reason, 'not on broken listing');
	assert.equal(row.etag, '');
	assert.equal(row.comments, null);
	assert.equal(row.genre, null);
	assert.equal(row.strip, null);
	assert.equal(row.rb_meta, null);
	assert.equal(row.match_context, null);
	assert.equal(row.quality, null);
	assert.equal(row.play_count, 0);
	assert.deepEqual(row.vocals, { status: 'not_analyzed' });
	assert.deepEqual(row.stems, { status: 'none' });
	assert.equal(row.has_rb_mapping, true);
	assert.equal(row.artwork_available, null);
	assert.equal(row.artwork_status, 'file_missing');
	assert.equal(row.revealed, false);
});

test('vendor_id presence is the only has_rb_mapping signal', () => {
	assert.equal(missing.brokenTrackToBrowserRow(brokenTrack({ vendor_id: '42' }), 1).has_rb_mapping, true);
	assert.equal(missing.brokenTrackToBrowserRow(brokenTrack({ vendor_id: null }), 2).has_rb_mapping, false);
	assert.equal(missing.brokenTrackToBrowserRow(brokenTrack({ vendor_id: '' }), 3).has_rb_mapping, false);
});

test('fetchMissingTrackRows calls GET /api/v1/reconcile/broken with no playlist_id', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({
			total: 1,
			tracks: [brokenTrack({ file_exists: false, is_streaming: false })]
		});
	};

	const result = await missing.fetchMissingTrackRows();

	assert.equal(seen.url, `${API_BASE}/api/v1/reconcile/broken`);
	assert.equal(seen.method, 'GET');
	assert.equal(result.truncated, false);
	assert.equal(result.etag, '');
	assert.equal(result.rows.length, 1);
	assert.equal(result.rows[0].order, 1);
	assert.equal(result.rows[0].file_exists, false);
	assert.equal(result.rows[0].is_streaming, false);
});
