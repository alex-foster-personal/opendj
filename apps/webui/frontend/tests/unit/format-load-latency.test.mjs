import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { formatLoadLatency } from '../../src/lib/rb/format-load-latency.ts';

describe('formatLoadLatency', () => {
	it('rounds seconds to one decimal', () => {
		assert.equal(formatLoadLatency(2289), '2.3s');
		assert.equal(formatLoadLatency(1000), '1.0s');
	});

	it('rounds sub-second to ~50ms steps', () => {
		assert.equal(formatLoadLatency(312), '~300ms');
		assert.equal(formatLoadLatency(20), '~50ms');
	});
});
