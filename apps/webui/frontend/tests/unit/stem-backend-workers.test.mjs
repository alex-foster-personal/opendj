import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	assertNoStemBackendWorkers,
	findStemBackendWorkers
} from '../e2e/support/stem-backend-workers.ts';

test('findStemBackendWorkers ignores unavailable telemetry', () => {
	assert.deepEqual(findStemBackendWorkers({ available: false }), []);
});

test('findStemBackendWorkers detects stems_local_worker argv', () => {
	const hits = findStemBackendWorkers({
		available: true,
		members: [{ command: ['python', '-m', 'apps.stems.stems_local_worker'] }]
	});
	assert.deepEqual(hits, ['stems_local_worker']);
});

test('assertNoStemBackendWorkers throws on hits', () => {
	assert.throws(
		() =>
			assertNoStemBackendWorkers({
				available: true,
				members: [{ name: 'stems_local_worker' }]
			}),
		/stems_local_worker/
	);
});

test('assertNoStemBackendWorkers throws when telemetry is unavailable, rather than passing', () => {
	assert.throws(() => assertNoStemBackendWorkers({ available: false }), /available/);
	assert.throws(() => assertNoStemBackendWorkers({}), /available/);
});
