// requirement: SET-13
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	REC_STATUS_UNREACHABLE_GROUP,
	REC_STATUS_UNREACHABLE_LIMIT,
	recorderStatusRetryMs,
	recorderStatusUnreachable,
	recorderStatusUnreachableMessage
} from './recorder-status-retry.ts';

// [if] fetch gets no answer [then] only its TypeError is classified as unreachable.
test('unreachable status reads are distinguished from answered errors', () => {
	assert.equal(recorderStatusUnreachable(new TypeError('Load failed')), true);
	assert.equal(recorderStatusUnreachable(new Error('503 Service Unavailable')), false);
	assert.equal(recorderStatusUnreachable('Load failed'), false);
	assert.equal(recorderStatusUnreachable(null), false);
});

// [if] consecutive reads fail [then] backoff doubles from 2 seconds up to 8 seconds.
test('status retry delays double and stay capped', () => {
	assert.deepEqual(
		[0, 1, 2, 3, 4, 20].map(recorderStatusRetryMs),
		[2000, 2000, 4000, 8000, 8000, 8000]
	);
});

// [if] three reads go unanswered [then] the grouped error retains the count and cause.
test('the grouped unreachable message preserves the runtime cause', () => {
	assert.equal(REC_STATUS_UNREACHABLE_LIMIT, 3);
	assert.equal(REC_STATUS_UNREACHABLE_GROUP, 'rec-status-unreachable');
	assert.equal(
		recorderStatusUnreachableMessage(3, new TypeError('Load failed')),
		'REC status: the engine is not answering (3 reads in a row): TypeError: Load failed'
	);
});
