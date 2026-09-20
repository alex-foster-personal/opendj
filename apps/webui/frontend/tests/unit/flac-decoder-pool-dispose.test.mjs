import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const pool = await loadTypeScriptModule('src/lib/player/decode/flac-decoder-pool.ts');

test('disposeStemDecoderPools frees live decoders', async () => {
	let freed = 0;
	const decoder = {
		ready: Promise.resolve(),
		decodeFile: async () => ({
			channelData: [new Float32Array(1)],
			samplesDecoded: 1,
			sampleRate: 44100
		}),
		reset: async () => {},
		free: async () => {
			freed += 1;
		}
	};
	const taken = await pool.takeDecoder(() => decoder, 'test-dispose');
	pool.returnDecoder(taken, 'test-dispose');
	assert.equal(pool.activeStemWorkerCount(), 1);
	await pool.disposeStemDecoderPools();
	assert.equal(pool.activeStemWorkerCount(), 0);
	assert.equal(pool.pooledCount('test-dispose'), 0);
	assert.equal(freed, 1);
});
