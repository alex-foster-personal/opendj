/**
 * Play analytics on a library that never recorded a set, against the real daemon.
 *
 * The real daemon app (fixtures/real_daemon_server.py) over a scratch data dir
 * has no event store, the exact state of a fresh install; the production client
 * `fetchPlayAnalytics` reads it through the generated API client, the daemon's
 * own HTTPException serialization and the `ApiError` path. Replaces a test that
 * answered the client with a hand-written 503 carrying the very text the client
 * matches on (PR #5054 review).
 *
 * - if a library that never recorded a set shows a red raw error, a fresh
 *   install looks broken -- broken.
 * - if every 503 reads as "nothing recorded", a broken store hides behind the
 *   empty state -- broken.
 */
import assert from 'node:assert/strict';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { startRealDaemon } from './fixtures/real-daemon.mjs';

let daemon;
let analyticsApi;

before(async () => {
	daemon = await startRealDaemon();
	analyticsApi = await loadTypeScriptModule('src/routes/play-analytics/analytics-api.ts', {
		viteApiBase: daemon.apiBase
	});
});

after(async () => {
	if (daemon) await daemon.stop();
});

test('a never-created event store reaches the client as NoEventStoreError', async () => {
	await assert.rejects(analyticsApi.fetchPlayAnalytics(null, 50), (error) => {
		assert.ok(
			analyticsApi.isNoEventStoreError(error),
			`expected the empty state, got ${error?.constructor?.name}: ${error?.message}`
		);
		return true;
	});
});

test('an event store that exists without its schema stays a loud failure', async () => {
	// The other real 503 the same route answers: a file is there, its tables are not.
	const setsDir = join(daemon.scratchDir, 'sets');
	mkdirSync(setsDir, { recursive: true });
	writeFileSync(join(setsDir, 'sets.db'), '');
	try {
		await assert.rejects(analyticsApi.fetchPlayAnalytics(null, 50), (error) => {
			assert.ok(!analyticsApi.isNoEventStoreError(error), 'a broken store is not an empty one');
			assert.match(
				error.message,
				/GET \/api\/play-analytics failed: 503 .*required table 'sets' is missing/
			);
			return true;
		});
	} finally {
		rmSync(join(setsDir, 'sets.db'));
	}
});
