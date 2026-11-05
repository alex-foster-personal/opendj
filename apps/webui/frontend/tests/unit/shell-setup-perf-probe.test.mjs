// PERFPROBE-02: the shell's setup page opens /performance with the frame probe
// only when the shell injected OPENDJ_PERF_PROBE = true.
import assert from 'node:assert/strict';
import test from 'node:test';

import { performancePagePath } from '../../../../desktop/setup/setup.js';

test('the setup page adds the frame probe only when the shell asked for it', () => {
	assert.equal(performancePagePath(true), '/performance?perfprobe=1');
	assert.equal(performancePagePath(false), '/performance');
	assert.equal(performancePagePath(undefined), '/performance');
	assert.equal(
		performancePagePath('1'),
		'/performance',
		'only the boolean the shells inject counts'
	);
});
