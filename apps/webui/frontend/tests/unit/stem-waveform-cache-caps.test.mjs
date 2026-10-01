import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let caps;

test('stem waveform LRU evicts the oldest ready entry past the entry cap', async () => {
	caps = await loadTypeScriptModule('src/lib/components/rb/wave/stem-waveform-cache-caps.ts');
	const cache = {};
	const cap = 12;
	for (let i = 0; i < cap + 1; i++) {
		cache[`track-${i}:vocals`] = {
			status: 'ready',
			touched: caps.nextStemWaveformTouch(),
			envelope: [0, 0.5, 1]
		};
	}
	caps.evictStemWaveformLru(cache, {
		entryCap: cap,
		byteCap: 128 * 1024 * 1024,
		estimateBytes: (entry) => (entry.envelope?.length ?? 0) * 8,
		isReady: (entry) => entry.status === 'ready'
	});
	assert.equal(Object.keys(cache).length, cap);
	assert.equal(cache['track-0:vocals'], undefined);
	assert.equal(cache[`track-${cap}:vocals`]?.status, 'ready');
});
