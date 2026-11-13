/**
 * Issue #13205: deduplication regressions for the vocals-aware ANLZ refresh
 * ((private path), "Outcome"). Repeated hydration with an
 * unchanged status must not issue cache-bypass requests; one status change
 * yields exactly one request that fans out to every loaded matching deck; a
 * later status change yields exactly one more. Real node:http server, a fresh
 * cache module per test, and the production deck sink (createDeckVocalsSink).
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let server;
let origin;
/** Deck-shaped holders the installed vocals sink writes through. */
let deckHolders = [];
/** stable_id -> { vocals } served by the real HTTP server. */
const anlzCatalog = new Map();
/** Every real /anlz request observed on the socket. */
const anlzRequestLog = [];

const demucsVocals = {
	status: 'demucs',
	fps: 10,
	regions: [{ start_s: 1, end_s: 2, intensity: 0.8 }]
};
const rekordboxVocals = {
	status: 'rekordbox',
	fps: 10,
	regions: [{ start_s: 3, end_s: 4, intensity: 2 }]
};

/** Minimal valid /anlz payload carrying `vocals` for `stable_id`. */
function anlzPayload(vocals, stable_id) {
	return {
		stable_id,
		points: 38400,
		waveform: {
			kind: 'mono',
			preview: { length: 0, low: [], mid: [], high: [] },
			detail: { length: 0, low: [], mid: [], high: [] }
		},
		beatgrid: { beat_count: 0, beats: [] },
		cues: [],
		phrases: [],
		local_waveform: { status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 },
		vocals
	};
}

/** Number of /anlz requests the server saw for `stableId`. */
function requestsFor(stableId) {
	return anlzRequestLog.filter((r) => r.stableId === stableId).length;
}

/** Loads a dedicated cache module with the production deck vocals sink installed. */
async function freshCache() {
	const cache = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache.svelte.ts', {
		viteApiBase: origin
	});
	const { createDeckVocalsSink } = await loadTypeScriptModule('src/lib/rb/deck-vocals-sink.ts');
	const deckAt = (deck) => deckHolders[Number(deck)];
	cache.installAuthoritativeAnlzVocalsSink(
		createDeckVocalsSink({
			deckIds: ['0', '1', '2', '3'],
			deckLoadToken: (deck) => deckAt(deck)?.loadToken ?? 0,
			deckStableId: (deck) => deckAt(deck)?.stable_id ?? null,
			deckAnlz: (deck) => deckAt(deck)?.anlz ?? null,
			publishDeckAnlz: (deck, anlz) => {
				deckAt(deck).anlz = anlz;
			}
		})
	);
	return cache;
}

/** Yields a few macrotasks so any (wrongly) started refresh would reach the server. */
async function idle() {
	await new Promise((resolve) => setTimeout(resolve, 20));
}

/** Waits until the shared cache entry carries `status`, then one more macrotask
 * so the sink's deck writes that follow the cache write have run too. */
async function refreshSettled(cache, stableId, status) {
	const deadline = Date.now() + 5000;
	while (cache.getAnlzEntry(stableId)?.data?.vocals?.status !== status) {
		assert.ok(Date.now() < deadline, `refresh of ${stableId} never published ${status}`);
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
	await new Promise((resolve) => setTimeout(resolve, 0));
}

before(async () => {
	server = createServer((req, res) => {
		const url = req.url ?? '';
		const anlzMatch = /^\/api\/v1\/tracks\/([^/]+)\/anlz/.exec(url);
		if (anlzMatch === null) {
			res.writeHead(404).end();
			return;
		}
		const stableId = decodeURIComponent(anlzMatch[1]);
		anlzRequestLog.push({ stableId, url });
		const entry = anlzCatalog.get(stableId);
		if (entry === undefined) {
			res.writeHead(404).end();
			return;
		}
		res.writeHead(200, { 'content-type': 'application/json' });
		res.end(JSON.stringify(anlzPayload(entry.vocals, stableId)));
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	origin = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
	await new Promise((resolve) => server.close(resolve));
});

// Many row observations across ticks with the cached status must stay silent.
test('repeated hydration with an unchanged vocal status issues no bypass request', async () => {
	const stableId = 'dedup-unchanged-track';
	const neverCached = 'dedup-never-cached-track';
	anlzCatalog.set(stableId, { vocals: demucsVocals });
	anlzCatalog.set(neverCached, { vocals: { status: 'not_analyzed' } });
	const loaded = anlzPayload(demucsVocals, stableId);
	deckHolders = [
		{ stable_id: stableId, anlz: loaded, loadToken: 0 },
		{ stable_id: stableId, anlz: loaded, loadToken: 0 }
	];
	const cache = await freshCache();
	try {
		cache.refreshAnlzCacheEntry(stableId, loaded);
		for (let i = 0; i < 5; i += 1) {
			cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
			cache.refreshAnlzVocalsIfNeeded(neverCached, 'not_analyzed');
			await idle();
		}
		assert.equal(requestsFor(stableId), 0, 'unchanged status must never bypass-fetch');
		assert.equal(requestsFor(neverCached), 0, 'uncached not_analyzed must never bypass-fetch');
		assert.equal(deckHolders[0].anlz, loaded, 'deck payload must not be republished');
		assert.equal(deckHolders[1].anlz, loaded, 'deck payload must not be republished');
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
		anlzCatalog.delete(neverCached);
	}
});

// Three decks hold the same stable id; repeated observations still make one request.
test('one changed status shared by several loaded decks fetches once and fans out to all', async () => {
	const stableId = 'dedup-fanout-track';
	const otherId = 'dedup-fanout-other-track';
	anlzCatalog.set(stableId, { vocals: demucsVocals });
	const stale = anlzPayload({ status: 'not_analyzed' }, stableId);
	const other = anlzPayload({ status: 'not_analyzed' }, otherId);
	deckHolders = [
		{ stable_id: stableId, anlz: stale, loadToken: 0 },
		{ stable_id: otherId, anlz: other, loadToken: 0 },
		{ stable_id: stableId, anlz: stale, loadToken: 0 },
		{ stable_id: stableId, anlz: stale, loadToken: 0 }
	];
	const cache = await freshCache();
	try {
		cache.refreshAnlzCacheEntry(stableId, stale);
		for (let i = 0; i < 4; i += 1) cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		await refreshSettled(cache, stableId, 'demucs');
		for (let i = 0; i < 4; i += 1) cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		await idle();
		assert.equal(requestsFor(stableId), 1, 'one status change must cost exactly one request');
		assert.match(anlzRequestLog.find((r) => r.stableId === stableId).url, /\/anlz\?/);
		for (const deck of [0, 2, 3]) {
			assert.equal(deckHolders[deck].anlz.vocals.status, 'demucs', `deck ${deck} must update`);
			assert.deepEqual(deckHolders[deck].anlz.vocals.regions, demucsVocals.regions);
		}
		assert.equal(deckHolders[1].anlz, other, 'a deck on another stable id stays untouched');
		assert.equal(requestsFor(otherId), 0);
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
	}
});

// not_analyzed -> demucs -> rekordbox costs two requests total, each fanned out.
test('a status that changes again after a successful refresh triggers exactly one more', async () => {
	const stableId = 'dedup-second-change-track';
	anlzCatalog.set(stableId, { vocals: demucsVocals });
	const stale = anlzPayload({ status: 'not_analyzed' }, stableId);
	deckHolders = [
		{ stable_id: stableId, anlz: stale, loadToken: 0 },
		{ stable_id: stableId, anlz: stale, loadToken: 0 }
	];
	const cache = await freshCache();
	try {
		cache.refreshAnlzCacheEntry(stableId, stale);
		cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		await refreshSettled(cache, stableId, 'demucs');
		assert.equal(requestsFor(stableId), 1);

		anlzCatalog.set(stableId, { vocals: rekordboxVocals });
		cache.refreshAnlzVocalsIfNeeded(stableId, 'rekordbox');
		cache.refreshAnlzVocalsIfNeeded(stableId, 'rekordbox');
		await refreshSettled(cache, stableId, 'rekordbox');
		cache.refreshAnlzVocalsIfNeeded(stableId, 'rekordbox');
		await idle();
		assert.equal(requestsFor(stableId), 2, 'the second change must cost exactly one request');
		for (const deck of [0, 1]) {
			assert.equal(deckHolders[deck].anlz.vocals.status, 'rekordbox', `deck ${deck} must update`);
			assert.deepEqual(deckHolders[deck].anlz.vocals.regions, rekordboxVocals.regions);
		}
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
	}
});
