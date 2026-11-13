/**
 * QLT hover fetch cache (QLTSCAN-05): `audioQualityHover` via requestCoalescer.
 * Twin badge mount coverage for issue #9320 lives in track-table-header-markup.test.mjs (GUARD-15: that file already allowlists Svelte source reads).
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

afterEach(async () => {
	if (originalFetch !== undefined) {
		globalThis.fetch = originalFetch;
		originalFetch = undefined;
	}
	await Promise.resolve();
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

/** A settled hover inside the TTL is served from the cache with no new fetch (issue #13138). */
test('cached result inside TTL is reused without a fetch', async () => {
	const host = makeHost();
	const coalescer = coalescerMod.createCoalescer({ now: host.now });
	let fetches = 0;
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		fetches += 1;
		return jsonResponse({ quality: { grade: { hover: `cached-${fetches}` } } });
	};

	const first = await hoverMod.audioQualityHover('t6', 'k', coalescer);
	host.advance(hoverMod.HOVER_TTL_MS - 1);
	const second = await hoverMod.audioQualityHover('t6', 'k', coalescer);
	assert.equal(first, 'cached-1');
	assert.equal(second, 'cached-1');
	assert.equal(fetches, 1);
});

/** A different track with the same grade key gets its own cache entry and fetch (issue #13138). */
test('track change does not reuse the previous track entry', async () => {
	const host = makeHost();
	const coalescer = coalescerMod.createCoalescer({ now: host.now });
	const urls = [];
	originalFetch = globalThis.fetch;
	globalThis.fetch = async (url) => {
		urls.push(String(url));
		return jsonResponse({ quality: { grade: { hover: `hover for ${urls.length}` } } });
	};

	const a = await hoverMod.audioQualityHover('track-a', '{"letter":"A"}', coalescer);
	const b = await hoverMod.audioQualityHover('track-b', '{"letter":"A"}', coalescer);
	assert.equal(urls.length, 2);
	assert.match(urls[0], /\/track-a$/);
	assert.match(urls[1], /\/track-b$/);
	assert.notEqual(a, b);
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
