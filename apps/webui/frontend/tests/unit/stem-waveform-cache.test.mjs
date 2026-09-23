import assert from 'node:assert/strict';
import { before, afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://stem-waveform-cache.example.test';

let cache;
const originalFetch = globalThis.fetch;

before(async () => {
	cache = await loadTypeScriptModule('src/lib/components/rb/wave/stem-waveform-cache.svelte.ts', {
		viteApiBase: API_BASE
	});
});

afterEach(() => {
	globalThis.fetch = originalFetch;
});

function envelopeResponse(stableId, part) {
	return new Response(
		JSON.stringify({
			schema: 1,
			stable_id: stableId,
			part,
			layout: 'demucs4',
			points: 512,
			envelope: [0, 0.5, 1, 0.25]
		}),
		{ status: 200, headers: { 'content-type': 'application/json' } }
	);
}

test('ensureStemWaveform stores a ready envelope from the server route', async () => {
	globalThis.fetch = async (url) => {
		assert.match(String(url), /tracks\/track-a\/stems\/vocals\/waveform$/);
		return envelopeResponse('track-a', 'vocals');
	};
	cache.ensureStemWaveform('track-a', 'vocals');
	await new Promise((resolve) => setTimeout(resolve, 0));
	const entry = cache.getStemWaveformEntry('track-a', 'vocals');
	assert.equal(entry?.status, 'ready');
	assert.ok(entry.envelope.length > 0);
});
