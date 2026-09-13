/**
 * Admin Diagnostics tab helpers.
 *
 * Regression lines:
 * - unknown tab query and missing query both resolve to kpi
 * - tab=diagnostics resolves to diagnostics
 * - pushHistory caps at 20 and keeps the newest
 * - staleness is true when lastOkAt is null or older than 60s, false at 30s
 * - entryFromHealth maps lock-free to lockHolder null and omits syncthing when absent
 * - well-formed ports body parses; a string backend throws; HTTP 503 surfaces status + body
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://admin-diagnostics.example.test';

let adminTab;
let healthHistory;
let diagnosticsApi;
let originalFetch;

function engineHealth(overrides = {}) {
	return {
		status: 'ok',
		state_db: {
			path: 'data/state/state.db',
			tracks: 100,
			playlists: 5,
			pairings: 0,
			last_writer_hostname: null,
			last_writer_at: null
		},
		cloud: { lock_holder: null },
		syncthing: null,
		bind_host: '127.0.0.1',
		version: '0.1.0',
		contract_rev: 'sha256:abc',
		engine_version: '0.1.0',
		boot_id: 'boot-1',
		...overrides
	};
}

before(async () => {
	adminTab = await loadTypeScriptModule('src/routes/admin/admin-tab.ts');
	healthHistory = await loadTypeScriptModule('src/routes/admin/health-history.ts');
	diagnosticsApi = await loadTypeScriptModule('src/routes/admin/diagnostics-api.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('missing tab query resolves to kpi', () => {
	assert.equal(adminTab.adminTabFromUrl(new URL('https://x.test/admin')), 'kpi');
});

test('unknown tab query resolves to kpi', () => {
	assert.equal(adminTab.adminTabFromUrl(new URL('https://x.test/admin?tab=other')), 'kpi');
});

test('tab=diagnostics resolves to diagnostics', () => {
	assert.equal(
		adminTab.adminTabFromUrl(new URL('https://x.test/admin?tab=diagnostics')),
		'diagnostics'
	);
});

test('pushHistory caps at 20 and keeps the newest', () => {
	let history = [];
	for (let i = 0; i < 25; i += 1) {
		history = healthHistory.pushHistory(history, {
			at: i,
			status: 'ok',
			tracks: i,
			playlists: 1,
			lockHolder: null,
			syncthingPeers: null,
			syncthingFolder: null,
			bindHost: '127.0.0.1'
		});
	}
	assert.equal(history.length, 20);
	assert.equal(history[0].at, 5);
	assert.equal(history[19].at, 24);
});

test('staleness is true when lastOkAt is null', () => {
	const result = healthHistory.staleness(null, Date.now());
	assert.equal(result.stale, true);
	assert.equal(result.ageMs, null);
});

test('staleness is true when older than 60s', () => {
	const now = Date.now();
	const result = healthHistory.staleness(now - 61_000, now);
	assert.equal(result.stale, true);
});

test('staleness is false at 30s', () => {
	const now = Date.now();
	const result = healthHistory.staleness(now - 30_000, now);
	assert.equal(result.stale, false);
});

test('entryFromHealth maps lock-free and absent syncthing', () => {
	const entry = healthHistory.entryFromHealth(engineHealth(), 123);
	assert.equal(entry.lockHolder, null);
	assert.equal(entry.syncthingPeers, null);
	assert.equal(entry.syncthingFolder, null);
	assert.equal(entry.tracks, 100);
});

test('a well-formed ports body parses', () => {
	const ports = diagnosticsApi.parseWorktreePorts({
		backend: 8680,
		frontend: 9400,
		api_proxy_target: 'http://127.0.0.1:8680'
	});
	assert.deepEqual(ports, {
		backend: 8680,
		frontend: 9400,
		api_proxy_target: 'http://127.0.0.1:8680'
	});
});

test('a string backend throws', () => {
	assert.throws(
		() =>
			diagnosticsApi.parseWorktreePorts({
				backend: '8680',
				frontend: 9400,
				api_proxy_target: 'http://127.0.0.1:8680'
			}),
		/backend is not an integer/
	);
});

test('HTTP 503 surfaces status and body', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: { code: 'worktree_ports_unreserved' } }), {
			status: 503,
			headers: { 'content-type': 'application/json' }
		});
	await assert.rejects(diagnosticsApi.fetchWorktreePorts(), /HTTP 503/);
});
