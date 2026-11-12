// requirement: ADMIN-12083
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let cloud;

before(async () => {
	cloud = await loadTypeScriptModule('src/lib/api-cloudsync.ts');
});

test('schedulerLastResult reads last_result from GET /api/v1/cloudsync/status', () => {
	const result = cloud.schedulerLastResult({
		enabled: true,
		configured: true,
		running: false,
		heartbeat_at: null,
		enabled_source: 'default',
		endpoint_source: 'default',
		reason: null,
		signed_in_as: null,
		last_push_at: null,
		last_pull_at: null,
		last_result: { status: 'ok', message: 'sync finished' },
		rows_pending: 0,
		hash_pending: 0,
		quarantined: 0,
		excluded_total: 0,
		endpoint: null,
		recent_results: [],
		update_required: null
	});
	assert.deepEqual(result, { status: 'ok', message: 'sync finished' });
	const empty = {
		enabled: false,
		configured: false,
		running: false,
		heartbeat_at: null,
		enabled_source: 'default',
		endpoint_source: 'default',
		reason: null,
		signed_in_as: null,
		last_push_at: null,
		last_pull_at: null,
		last_result: null,
		rows_pending: null,
		hash_pending: null,
		quarantined: null,
		excluded_total: null,
		endpoint: null,
		recent_results: [],
		update_required: null
	};
	assert.equal(cloud.schedulerLastResult(empty), null);
});
