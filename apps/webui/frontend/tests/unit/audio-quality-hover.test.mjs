/**
 * QLT hover fetch cache (QLTSCAN-05): `audioQualityHover` via requestCoalescer.
 */
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let coalescerMod;
let hoverMod;
let originalFetch;

function makeHost() {
	let clock = 0;
	return {
		now: () => clock,
		advance(ms) {
			clock += ms;
		}
	};
}

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'Content-Type': 'application/json' }
	});
}

before(async () => {
	coalescerMod = await loadTypeScriptModule('src/lib/api/request-coalescer.ts');
	hoverMod = await loadTypeScriptModule('src/lib/rb/api-audio-quality.ts');
});

afterEach(() => {
	if (originalFetch !== undefined) {
		globalThis.fetch = originalFetch;
	}
});

test('same key inside TTL shares one fetch', async () => {
	const host = makeHost();
	const coalescer = coalescerMod.createCoalescer({ now: host.now });
	let fetches = 0;
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		fetches += 1;
		return jsonResponse({ quality: { grade: { hover: 'Full hover text' } } });
	};

	const p1 = hoverMod.audioQualityHover('track-1', '{"letter":"A"}', coalescer);
	const p2 = hoverMod.audioQualityHover('track-1', '{"letter":"A"}', coalescer);
	const [a, b] = await Promise.all([p1, p2]);
	assert.equal(fetches, 1);
	assert.equal(a, 'Full hover text');
	assert.equal(b, 'Full hover text');
});

test('after TTL a second fetch runs', async () => {
	const host = makeHost();
	const coalescer = coalescerMod.createCoalescer({ now: host.now });
	let fetches = 0;
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		fetches += 1;
		return jsonResponse({ quality: { grade: { hover: `hover-${fetches}` } } });
	};

	const first = await hoverMod.audioQualityHover('t2', 'k', coalescer);
	assert.equal(first, 'hover-1');
	host.advance(hoverMod.HOVER_TTL_MS);
	const second = await hoverMod.audioQualityHover('t2', 'k', coalescer);
	assert.equal(second, 'hover-2');
	assert.equal(fetches, 2);
});

test('different keys do not share a fetch', async () => {
	const host = makeHost();
	const coalescer = coalescerMod.createCoalescer({ now: host.now });
	let fetches = 0;
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		fetches += 1;
		return jsonResponse({ quality: { grade: { hover: 'x' } } });
	};

	await hoverMod.audioQualityHover('t3', 'key-a', coalescer);
	await hoverMod.audioQualityHover('t3', 'key-b', coalescer);
	assert.equal(fetches, 2);
});

test('failed fetch is worded and not cached', async () => {
	const host = makeHost();
	const coalescer = coalescerMod.createCoalescer({ now: host.now });
	let fetches = 0;
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		fetches += 1;
		if (fetches === 1) return new Response('nope', { status: 500 });
		return jsonResponse({ quality: { grade: { hover: 'recovered' } } });
	};

	const failed = await hoverMod.audioQualityHover('t4', 'k', coalescer);
	assert.match(failed, /^Audio quality details could not be loaded:/);
	const ok = await hoverMod.audioQualityHover('t4', 'k', coalescer);
	assert.equal(ok, 'recovered');
	assert.equal(fetches, 2);
});

test('two-arg call runs without throwing', async () => {
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => jsonResponse({ quality: { grade: { hover: 'singleton path' } } });
	const text = await hoverMod.audioQualityHover('t5', '{"letter":"B"}');
	assert.equal(text, 'singleton path');
});
