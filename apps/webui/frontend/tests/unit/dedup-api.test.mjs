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
						artist: 'the maintainer',
						bpm: 124,
						key: '8A',
						duration_ms: 210000,
						rating: 4,
						file_exists: true
					}
				],
				decision:
					decisionAction === null
						? null
						: {
								cluster_key: CLUSTER_KEY,
								survivor: 'track-canon',
								action: decisionAction,
								decided_at: '2026-07-22T12:00:00.000000Z'
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
	globalThis.fetch = async (input, init) => {
		request = { input: String(input), init };
		return new Response(JSON.stringify(clusterPayload()), {
			status: 200,
			headers: { 'content-type': 'application/json', etag: INITIAL_REVISION }
		});
	};

	const response = await dedupApi.fetchDedupClusters(controller.signal);

	assert.equal(request.input, `${API_BASE}/api/v1/dedup/clusters`);
	assert.equal(request.init.signal, controller.signal);
	assert.equal(response.revision, INITIAL_REVISION);
	assert.equal(response.clusters[0].cluster_key, CLUSTER_KEY);
	assert.equal(dedupApi.dedupArtworkUrl('track/id'), `${API_BASE}/api/v1/tracks/track%2Fid/artwork?size=s`);
});

test('postDedupDecision binds stable identity and CAS revision', async () => {
	const controller = new AbortController();
	let request;
	globalThis.fetch = async (input, init) => {
		request = { input: String(input), init };
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

	assert.equal(request.input, `${API_BASE}/api/v1/dedup/clusters/7/decision`);
	assert.equal(request.init.signal, controller.signal);
	assert.equal(new Headers(request.init.headers).get('if-match'), INITIAL_REVISION);
	assert.deepEqual(JSON.parse(request.init.body), {
		cluster_key: CLUSTER_KEY,
		survivor: 'track-canon',
		action: 'merge'
	});
	assert.equal(record.revision, NEXT_REVISION);
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
