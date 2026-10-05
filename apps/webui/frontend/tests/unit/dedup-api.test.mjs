import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://dedup-api.example.test';
const CLUSTER_KEY = `sha256:${'a'.repeat(64)}`;
const INITIAL_REVISION = `"${'b'.repeat(64)}"`;
const NEXT_REVISION = `"${'c'.repeat(64)}"`;

let dedupApi;
let originalFetch;

function clusterPayload({ decisionAction = null } = {}) {
	return {
		clusters: [
			{
				cluster_id: 7,
				cluster_key: CLUSTER_KEY,
				survivor_stable_id: 'track-canon',
				rationale: 'bitrate=320',
				flagged_manual_review: false,
				members: [
					{
						stable_id: 'track-canon',
						path: '/music/canon.flac',
						is_canonical: true,
						similarity: null,
						title: 'Midnight Drive',
						artist: 'Tamsin Quell',
						bpm: 124,
						key: '8A',
						duration_ms: 210000,
						rating: 4,
						file_exists: true,
						cue_count: 0,
						hot_cue_count: 0,
						loop_count: 0,
						has_beatgrid: false,
						cue_positions_ms: []
					}
				],
				decision:
					decisionAction === null
						? null
						: {
								cluster_key: CLUSTER_KEY,
								survivor: 'track-canon',
								action: decisionAction,
								decided_at: '2026-07-22T12:00:00.000000Z',
								pending_apply: true
							}
			}
		],
		note: null,
		revision: INITIAL_REVISION
	};
}

before(async () => {
	dedupApi = await loadTypeScriptModule('src/routes/dedup/dedup-api.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('fetchDedupClusters uses configured base and forwards cancellation', async () => {
	const controller = new AbortController();
	let request;
	// openapi-fetch calls fetch(request) with one Request object, so the URL,
	// headers, body and signal are read off the request rather than an init bag.
	globalThis.fetch = async (input) => {
		request = input;
		return new Response(JSON.stringify(clusterPayload()), {
			status: 200,
			headers: { 'content-type': 'application/json', etag: INITIAL_REVISION }
		});
	};

	const response = await dedupApi.fetchDedupClusters(controller.signal);

	assert.equal(request.url, `${API_BASE}/api/v1/dedup/clusters`);
	assert.equal(request.signal.aborted, false);
	controller.abort();
	assert.equal(request.signal.aborted, true, 'caller cancellation must reach the request');
	assert.equal(response.revision, INITIAL_REVISION);
	assert.equal(response.clusters[0].cluster_key, CLUSTER_KEY);
	assert.equal(dedupApi.dedupArtworkUrl('track/id'), `${API_BASE}/api/v1/tracks/track%2Fid/artwork?size=s`);
});

test('postDedupDecision binds stable identity and CAS revision', async () => {
	const controller = new AbortController();
	let request;
	let body;
	globalThis.fetch = async (input) => {
		request = input;
		body = await input.clone().json();
		return new Response(
			JSON.stringify({
				cluster_id: 7,
				cluster_key: CLUSTER_KEY,
				survivor: 'track-canon',
				action: 'merge',
				decided_at: '2026-07-22T12:00:00.000000Z',
				pending_apply: true,
				revision: NEXT_REVISION
			}),
			{
				status: 200,
				headers: { 'content-type': 'application/json', etag: NEXT_REVISION }
			}
		);
	};

	const record = await dedupApi.postDedupDecision(
		7,
		CLUSTER_KEY,
		'track-canon',
		'merge',
		INITIAL_REVISION,
		controller.signal
	);

	assert.equal(request.url, `${API_BASE}/api/v1/dedup/clusters/7/decision`);
	assert.equal(request.method, 'POST');
	assert.equal(request.headers.get('if-match'), INITIAL_REVISION);
	assert.equal(request.signal.aborted, false);
	controller.abort();
	assert.equal(request.signal.aborted, true, 'caller cancellation must reach the request');
	assert.deepEqual(body, {
		cluster_key: CLUSTER_KEY,
		survivor: 'track-canon',
		action: 'merge'
	});
	assert.equal(record.revision, NEXT_REVISION);
});

test('a 409 becomes a conflict carrying the daemon revision to retry against', async () => {
	globalThis.fetch = async () =>
		new Response(null, {
			status: 409,
			statusText: 'Conflict',
			headers: { etag: NEXT_REVISION }
		});

	const caught = await dedupApi
		.postDedupDecision(7, CLUSTER_KEY, 'track-canon', 'merge', INITIAL_REVISION)
		.then(
			() => null,
			(error) => error
		);

	assert.ok(caught instanceof dedupApi.DedupConflictError, 'expected a DedupConflictError');
	assert.equal(caught.revision, NEXT_REVISION);
});

test('an unreachable daemon is reported as unreachable, not as a bad response', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('fetch failed');
	};

	await assert.rejects(dedupApi.fetchDedupClusters(), /daemon unreachable \(fetch failed\)/);
});

test('response validation rejects an unknown irreversible action', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify(clusterPayload({ decisionAction: 'delete-everything' })), {
			status: 200,
			headers: { 'content-type': 'application/json', etag: INITIAL_REVISION }
		});

	await assert.rejects(
		dedupApi.fetchDedupClusters(),
		/dedup: cluster 7\.decision\.action is not a supported action/
	);
});

test('response validation rejects an ETag/body revision mismatch', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify(clusterPayload()), {
			status: 200,
			headers: { 'content-type': 'application/json', etag: NEXT_REVISION }
		});

	await assert.rejects(dedupApi.fetchDedupClusters(), /dedup: response revision does not match ETag/);
});

test('decision response cannot claim that irreversible apply already ran', async () => {
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({
				cluster_id: 7,
				cluster_key: CLUSTER_KEY,
				survivor: 'track-canon',
				action: 'merge',
				decided_at: '2026-07-22T12:00:00.000000Z',
				pending_apply: false,
				revision: NEXT_REVISION
			}),
			{ status: 200, headers: { 'content-type': 'application/json', etag: NEXT_REVISION } }
		);

	await assert.rejects(
		dedupApi.postDedupDecision(
			7,
			CLUSTER_KEY,
			'track-canon',
			'merge',
			INITIAL_REVISION
		),
		/dedup: decision response must remain pending apply/
	);
});

test('GET with two clusters sharing cluster_key throws', async () => {
	const payload = clusterPayload();
	payload.clusters.push({ ...payload.clusters[0], cluster_id: 8 });
	globalThis.fetch = async () =>
		new Response(JSON.stringify(payload), {
			status: 200,
			headers: { 'content-type': 'application/json', etag: INITIAL_REVISION }
		});

	await assert.rejects(dedupApi.fetchDedupClusters(), /dedup: duplicate cluster_key/);
});

test('applyDedupMerge posts apply with If-Match and accepts pending_apply false', async () => {
	const controller = new AbortController();
	let request;
	let body;
	globalThis.fetch = async (input) => {
		request = input;
		body = await input.clone().json();
		return new Response(
			JSON.stringify({
				cluster_id: 7,
				cluster_key: CLUSTER_KEY,
				survivor: 'track-canon',
				action: 'merge',
				decided_at: '2026-07-22T12:00:00.000000Z',
				pending_apply: false,
				revision: NEXT_REVISION,
				playlists: [
					{
						playlist_id: 'pl-1',
						name: 'Warmup',
						before: ['track-alias'],
						after: ['track-canon']
					}
				]
			}),
			{
				status: 200,
				headers: { 'content-type': 'application/json', etag: NEXT_REVISION }
			}
		);
	};

	const record = await dedupApi.applyDedupMerge(
		7,
		CLUSTER_KEY,
		'track-canon',
		INITIAL_REVISION,
		controller.signal
	);

	assert.equal(request.url, `${API_BASE}/api/v1/dedup/clusters/7/apply`);
	assert.equal(request.method, 'POST');
	assert.equal(request.headers.get('if-match'), INITIAL_REVISION);
	assert.deepEqual(body, {
		cluster_key: CLUSTER_KEY,
		survivor: 'track-canon',
		confirm_cue_loss: false
	});
	assert.equal(record.pending_apply, false);
	assert.equal(record.playlists[0].playlist_id, 'pl-1');
});

test('undoDedupMerge posts undo and a 409 becomes DedupConflictError', async () => {
	let request;
	globalThis.fetch = async (input) => {
		request = input;
		return new Response(
			JSON.stringify({
				cluster_id: 7,
				cluster_key: CLUSTER_KEY,
				survivor: 'track-canon',
				action: 'merge',
				decided_at: '2026-07-22T12:00:00.000000Z',
				pending_apply: true,
				revision: NEXT_REVISION,
				playlist_ids: ['pl-1']
			}),
			{
				status: 200,
				headers: { 'content-type': 'application/json', etag: NEXT_REVISION }
			}
		);
	};

	const record = await dedupApi.undoDedupMerge(7, CLUSTER_KEY, 'track-canon', INITIAL_REVISION);
	assert.equal(request.url, `${API_BASE}/api/v1/dedup/clusters/7/undo`);
	assert.equal(record.pending_apply, true);

	globalThis.fetch = async () =>
		new Response(null, {
			status: 409,
			statusText: 'Conflict',
			headers: { etag: NEXT_REVISION }
		});

	const caught = await dedupApi
		.applyDedupMerge(7, CLUSTER_KEY, 'track-canon', INITIAL_REVISION)
		.then(
			() => null,
			(error) => error
		);
	assert.ok(caught instanceof dedupApi.DedupConflictError, 'expected a DedupConflictError');
	assert.equal(caught.revision, NEXT_REVISION);
});

test('fetchDedupClusters rejects members missing cue_count', async () => {
	const payload = clusterPayload();
	delete payload.clusters[0].members[0].cue_count;
	globalThis.fetch = async () =>
		new Response(JSON.stringify(payload), {
			status: 200,
			headers: { 'content-type': 'application/json', etag: INITIAL_REVISION }
		});

	await assert.rejects(dedupApi.fetchDedupClusters(), /cue_count/);
});
