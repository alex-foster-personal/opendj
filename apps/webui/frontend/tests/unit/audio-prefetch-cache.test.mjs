import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// H18 - hard-cap the row-select audio prefetch at 4 tracks AND 48 MiB. This is
// the primary RAM brake on a machine that already thrashes, and nothing guarded
// it. No mocked fetch here: a real node:http server serves real bytes over a
// real socket, so the cache under test does real work.
//
// Regression lines:
// - if a 5th ready track survives then the track cap is gone
// - if the evicted entry is not the least-recently-used then browse-ahead thrashes
// - if cumulative ready bytes pass MAX_AUDIO_PREFETCH_BYTES then the RAM brake is gone
// - if an over-budget buffer is retained alongside others then one track blows the budget
// - if a failed fetch retains bytes then errors leak RAM
// - if copyPrefetchedAudio hands back the cached buffer then decodeAudioData detaches it

const MiB = 1024 * 1024;

let server;
let origin;
/** stable_id -> {size} or {httpStatus} - what the real server will serve. */
const catalog = new Map();

before(async () => {
	server = createServer((req, res) => {
		const match = /^\/api\/v1\/tracks\/([^/]+)\/audio$/.exec(req.url ?? '');
		const entry = match === null ? undefined : catalog.get(decodeURIComponent(match[1]));
		if (entry === undefined) {
			res.writeHead(404).end();
			return;
		}
		if (entry.httpStatus !== undefined) {
			res.writeHead(entry.httpStatus).end();
			return;
		}
		// Deterministic real bytes: first byte identifies the track.
		const body = Buffer.alloc(entry.size, entry.fill ?? 0x41);
		res.writeHead(200, { 'Content-Type': 'audio/mpeg', 'Content-Length': body.length });
		res.end(body);
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	origin = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
	await new Promise((resolve) => server.close(resolve));
});

/** A fresh module instance so each test starts with an empty cache. */
async function freshCache() {
	return loadTypeScriptModule('src/lib/rb/audio-prefetch-cache.svelte.ts', {
		viteApiBase: origin
	});
}

function serve(stable_id, size, fill = 0x41) {
	catalog.set(stable_id, { size, fill });
}

/** Kick a prefetch and wait for it to reach a terminal status. */
async function prefetchAndSettle(cache, stable_id, timeoutMs = 20_000) {
	cache.ensureAudioPrefetch(stable_id);
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		const status = cache.audioPrefetchStatus(stable_id);
		if (status === 'ready' || status === 'error') return status;
		if (Date.now() > deadline) {
			throw new Error(`prefetch of ${stable_id} never settled (status ${String(status)})`);
		}
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}

// --------------------------------------------------------------- track cap

test('a 5th prefetched track evicts the least-recently-used one, capping ready at 4', async () => {
	const cache = await freshCache();
	assert.equal(cache.MAX_AUDIO_PREFETCH_TRACKS(), 4);

	for (const id of ['t1', 't2', 't3', 't4']) {
		serve(id, 64 * 1024);
		assert.equal(await prefetchAndSettle(cache, id), 'ready');
	}
	assert.equal(cache.audioPrefetchReadyCount(), 4);

	// Touch t1 so t2 becomes the least recently used.
	assert.notEqual(cache.copyPrefetchedAudio('t1'), null);

	serve('t5', 64 * 1024);
	assert.equal(await prefetchAndSettle(cache, 't5'), 'ready');

	assert.equal(
		cache.audioPrefetchReadyCount(),
		4,
		'ready count must never exceed MAX_AUDIO_PREFETCH_TRACKS'
	);
	assert.equal(cache.audioPrefetchStatus('t2'), undefined, 't2 was the LRU and must be evicted');
	assert.equal(cache.audioPrefetchStatus('t1'), 'ready', 'a touched entry must survive eviction');
	for (const id of ['t3', 't4', 't5']) {
		assert.equal(cache.audioPrefetchStatus(id), 'ready', `${id} must still be cached`);
	}
});

// ---------------------------------------------------------------- byte cap

test('the byte budget evicts before the track cap would, on big files', async () => {
	const cache = await freshCache();
	assert.equal(cache.MAX_AUDIO_PREFETCH_BYTES(), 48 * MiB);

	// 3 x 17 MiB = 51 MiB: over budget with only three entries, so the byte
	// brake must fire while the track count is still under 4.
	const big = 17 * MiB;
	for (const id of ['b1', 'b2', 'b3']) {
		serve(id, big);
		assert.equal(await prefetchAndSettle(cache, id), 'ready');
		assert.ok(
			cache.audioPrefetchReadyBytes() <= cache.MAX_AUDIO_PREFETCH_BYTES(),
			`retained ${cache.audioPrefetchReadyBytes()} bytes after ${id}, budget is ` +
				`${cache.MAX_AUDIO_PREFETCH_BYTES()}`
		);
	}
	assert.ok(
		cache.audioPrefetchReadyCount() < cache.MAX_AUDIO_PREFETCH_TRACKS(),
		'the byte budget must bite before the track cap on 17 MiB files, got ' +
			`${cache.audioPrefetchReadyCount()} entries`
	);
	assert.equal(cache.audioPrefetchStatus('b1'), undefined, 'b1 was the LRU and must be evicted');
	assert.equal(cache.audioPrefetchStatus('b3'), 'ready');
});

test('a single over-budget buffer is never retained alongside anything else', async () => {
	const cache = await freshCache();

	serve('small', 64 * 1024);
	assert.equal(await prefetchAndSettle(cache, 'small'), 'ready');

	// Larger than the whole budget: it may only ever be held alone.
	serve('huge', cache.MAX_AUDIO_PREFETCH_BYTES() + MiB);
	assert.equal(await prefetchAndSettle(cache, 'huge'), 'ready');

	assert.equal(
		cache.audioPrefetchStatus('small'),
		undefined,
		'an over-budget buffer must not stack on top of existing entries'
	);
	assert.equal(
		cache.audioPrefetchReadyCount(),
		1,
		'an over-budget buffer may only ever be retained alone'
	);

	// And the next normal insert must throw the oversized one out again.
	serve('after', 64 * 1024);
	assert.equal(await prefetchAndSettle(cache, 'after'), 'ready');
	assert.equal(cache.audioPrefetchStatus('huge'), undefined);
	assert.ok(cache.audioPrefetchReadyBytes() <= cache.MAX_AUDIO_PREFETCH_BYTES());
});

test('LOW tier evicts on third track with 24 MiB byte cap', async () => {
	const cache = await loadTypeScriptModule(
		'tests/unit/fixtures/audio-prefetch-cache-tier.ts',
		{ viteApiBase: origin }
	);
	cache.applyTier('LOW');
	assert.equal(cache.MAX_AUDIO_PREFETCH_TRACKS(), 2);
	for (const id of ['l1', 'l2', 'l3']) {
		serve(id, 8 * MiB);
		assert.equal(await prefetchAndSettle(cache, id), 'ready');
	}
	assert.equal(cache.audioPrefetchReadyCount(), 2);
	assert.ok(cache.audioPrefetchReadyBytes() <= 24 * MiB);
});

// ------------------------------------------------------- failures hold none

test('a failed prefetch is recorded as error and retains no bytes', async () => {
	const cache = await freshCache();

	serve('ok1', 64 * 1024);
	assert.equal(await prefetchAndSettle(cache, 'ok1'), 'ready');
	const bytesBefore = cache.audioPrefetchReadyBytes();

	catalog.set('boom', { httpStatus: 500 });
	assert.equal(await prefetchAndSettle(cache, 'boom'), 'error');

	assert.equal(cache.audioPrefetchReadyBytes(), bytesBefore, 'an error must retain no bytes');
	assert.equal(cache.audioPrefetchReadyCount(), 1);
	assert.equal(cache.copyPrefetchedAudio('boom'), null, 'an errored entry has nothing to decode');
});

test('a never-requested track reports undefined rather than a fabricated state', async () => {
	const cache = await freshCache();
	assert.equal(cache.audioPrefetchStatus('never-asked-for'), undefined);
	assert.equal(cache.copyPrefetchedAudio('never-asked-for'), null);
});

// ------------------------------------------------------------ copy on read

test('copyPrefetchedAudio hands out a copy so decode cannot detach the cache', async () => {
	const cache = await freshCache();
	serve('c1', 4096, 0x5a);

	assert.equal(await prefetchAndSettle(cache, 'c1'), 'ready');
	const first = cache.copyPrefetchedAudio('c1');
	assert.equal(first.byteLength, 4096);
	assert.equal(new Uint8Array(first)[0], 0x5a, 'the real served bytes must come back');

	// Scribble on the copy the way a decoder would.
	new Uint8Array(first).fill(0x00);

	const second = cache.copyPrefetchedAudio('c1');
	assert.notEqual(second, first, 'each read must be a distinct buffer');
	assert.equal(
		new Uint8Array(second)[0],
		0x5a,
		'mutating one copy must not corrupt the cached bytes'
	);
});

test('re-requesting a ready track does not refetch it', async () => {
	const cache = await freshCache();
	serve('r1', 4096);
	assert.equal(await prefetchAndSettle(cache, 'r1'), 'ready');

	// Take the track off the server entirely: a refetch would now 404.
	catalog.delete('r1');
	cache.ensureAudioPrefetch('r1');
	await new Promise((resolve) => setTimeout(resolve, 50));

	assert.equal(cache.audioPrefetchStatus('r1'), 'ready', 'a cache hit must not re-hit the network');
});

// ---------------------------------------------------------- clear (PERFMODE-05)

test('clearAudioPrefetchCache drops ready count and bytes to zero', async () => {
	const cache = await freshCache();
	for (const id of ['c1', 'c2', 'c3', 'c4']) {
		serve(id, 64 * 1024);
		assert.equal(await prefetchAndSettle(cache, id), 'ready');
	}
	assert.equal(cache.audioPrefetchReadyCount(), 4);
	assert.ok(cache.audioPrefetchReadyBytes() > 0);

	cache.clearAudioPrefetchCache();
	assert.equal(cache.audioPrefetchReadyCount(), 0);
	assert.equal(cache.audioPrefetchReadyBytes(), 0);
});

test('ensureAudioPrefetch still works after clearAudioPrefetchCache', async () => {
	const cache = await freshCache();
	serve('after-clear', 4096);
	cache.clearAudioPrefetchCache();
	assert.equal(await prefetchAndSettle(cache, 'after-clear'), 'ready');
	assert.equal(cache.audioPrefetchReadyCount(), 1);
});
