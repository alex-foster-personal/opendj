import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let formatLoadLatency;

before(async () => {
	({ formatLoadLatency } = await loadTypeScriptModule('src/lib/rb/format-load-latency.ts'));
});

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
