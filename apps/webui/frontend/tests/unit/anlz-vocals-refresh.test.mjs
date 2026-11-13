/**
 * Issue #1031: refresh client ANLZ vocals when hydrated library data reports a
 * newer vocal status than the shared cache. Uses a real node:http server and a
 * dedicated cache module instance per test, with the production deck sink
 * (createDeckVocalsSink, the factory audio-engine.svelte.ts installs) wired to
 * plain deck records through its ports.
 *
 * Issue #13206 (spec 7e3f2964 "Outcome"): the bypass refresh republishes only a
 * validated payload; a malformed vocals answer or a failed request fails loudly
 * and leaves the shared cache entry and every deck on their existing state.
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let server;
let origin;
/** Deck-shaped holders the installed vocals sink writes through. */
let deckHolders = [];
/** stable_id -> { vocals } or { fail: 'http500' | 'socket' } served by the real HTTP server. */
const anlzCatalog = new Map();
/** stable_id -> { status } served by GET /vocals/{id}/status. */
const vocalStatusCatalog = new Map();
/** Every real /anlz request observed on the socket. */
const anlzRequestLog = [];

function anlzPayload(vocals, stable_id = 'vocals-track') {
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

/** Waits for the fire-and-forget refresh to publish `status` into the shared
 * cache (fixed sleeps flake on a loaded runner), then one more macrotask so the
 * sink's deck writes that follow the cache write have run too. */
async function refreshSettled(cache, stableId, status = 'demucs') {
	const deadline = Date.now() + 5000;
	while (cache.getAnlzEntry(stableId)?.data?.vocals?.status !== status) {
		assert.ok(Date.now() < deadline, `refresh of ${stableId} never published ${status}`);
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
	await new Promise((resolve) => setTimeout(resolve, 0));
}

/** Runs `start` and resolves with the loud failure the fire-and-forget refresh
 * raises. Node's test runner attributes any unhandled rejection to the running
 * test even with an extra listener, so its own listener is parked for the
 * duration and restored afterwards. */
async function loudFailureOf(start) {
	const parked = process.listeners('unhandledRejection');
	process.removeAllListeners('unhandledRejection');
	try {
		const failure = new Promise((resolve, reject) => {
			const timer = setTimeout(() => reject(new Error('refresh never failed loudly')), 5000);
			process.once('unhandledRejection', (reason) => {
				clearTimeout(timer);
				resolve(reason);
			});
		});
		start();
		return await failure;
	} finally {
		process.removeAllListeners('unhandledRejection');
		for (const listener of parked) process.on('unhandledRejection', listener);
	}
}

/** Two decks holding the same track on a validated not_analyzed payload, with
 * that payload published as the shared cache entry. */
async function seededTwoDecks(stableId) {
	deckHolders = [
		{ stable_id: stableId, anlz: anlzPayload({ status: 'not_analyzed' }, stableId), loadToken: 0 },
		{ stable_id: stableId, anlz: anlzPayload({ status: 'not_analyzed' }, stableId), loadToken: 0 }
	];
	const cache = await freshCache();
	await cache.refreshAnlzCacheEntry(stableId, deckHolders[0].anlz);
	const seeded = cache.getAnlzEntry(stableId).data;
	return { cache, seeded, before: deckHolders.map((deck) => deck.anlz) };
}

/** Asserts the shared entry and both decks still carry exactly the pre-refresh state. */
function assertStatePreserved(cache, stableId, seeded, beforeDecks) {
	const entry = cache.getAnlzEntry(stableId);
	assert.equal(entry.status, 'ready', 'a failed refresh must not blank the good entry');
	assert.equal(entry.data, seeded, 'the shared entry must stay the validated payload');
	assert.equal(entry.data.vocals.status, 'not_analyzed');
	deckHolders.forEach((deck, i) => {
		assert.equal(deck.anlz, beforeDecks[i], `deck ${i} must keep its existing ANLZ object`);
		assert.equal(deck.anlz.vocals.status, 'not_analyzed');
	});
}

const DEMUCS_VOCALS = {
	status: 'demucs',
	fps: 10,
	regions: [{ start_s: 1, end_s: 2.5, intensity: 0.8 }]
};

/** After a failure the in-flight slot must be free: a later valid answer republishes. */
async function assertRecoversWithValidPayload(cache, stableId) {
	anlzCatalog.set(stableId, { vocals: DEMUCS_VOCALS });
	cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
	await refreshSettled(cache, stableId, 'demucs');
	assert.equal(anlzRequestLog.length, 2, 'the retry after a failure issues one new bypass fetch');
	assert.deepEqual(cache.getAnlzEntry(stableId).data.vocals, DEMUCS_VOCALS);
	for (const deck of deckHolders) assert.deepEqual(deck.anlz.vocals, DEMUCS_VOCALS);
}

before(async () => {
	server = createServer((req, res) => {
		const url = req.url ?? '';
		const anlzMatch = /^\/api\/v1\/tracks\/([^/]+)\/anlz/.exec(url);
		if (anlzMatch !== null) {
			const stableId = decodeURIComponent(anlzMatch[1]);
			anlzRequestLog.push({ stableId, url, cache: req.headers['cache-control'] ?? null });
			const entry = anlzCatalog.get(stableId);
			if (entry === undefined) {
				res.writeHead(404).end();
				return;
			}
			if (entry.fail === 'socket') {
				req.socket.destroy();
				return;
			}
			if (entry.fail === 'http500') {
				res.writeHead(500, { 'content-type': 'application/json' });
				res.end(
					JSON.stringify({ detail: { code: 'INTERNAL_ERROR', message: 'anlz build failed' } })
				);
				return;
			}
			res.writeHead(200, { 'content-type': 'application/json' });
			res.end(JSON.stringify(anlzPayload(entry.vocals, stableId)));
			return;
		}
		const statusMatch = /^\/api\/v1\/vocals\/([^/]+)\/status$/.exec(url);
		if (statusMatch !== null) {
			const stableId = decodeURIComponent(statusMatch[1]);
			const entry = vocalStatusCatalog.get(stableId);
			if (entry === undefined) {
				res.writeHead(404).end();
				return;
			}
			res.writeHead(200, { 'content-type': 'application/json' });
			res.end(JSON.stringify({ stable_id: stableId, status: entry.status, fps: 10, regions: [] }));
			return;
		}
		res.writeHead(404).end();
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	origin = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
	await new Promise((resolve) => server.close(resolve));
});

test('refreshAnlzVocalsIfNeeded bypass-fetches once and updates every duplicate deck holder', async () => {
	const stableId = 'dup-vocals-track';
	const demucsVocals = {
		status: 'demucs',
		fps: 10,
		regions: [{ start_s: 1, end_s: 2, intensity: 0.8 }]
	};
	anlzCatalog.set(stableId, { vocals: demucsVocals });
	anlzRequestLog.length = 0;
	deckHolders = [
		{ stable_id: stableId, anlz: anlzPayload({ status: 'not_analyzed' }, stableId), loadToken: 0 },
		{ stable_id: stableId, anlz: anlzPayload({ status: 'not_analyzed' }, stableId), loadToken: 0 },
		{ stable_id: null, anlz: null, loadToken: 0 }
	];
	const cache = await freshCache();
	try {
		cache.refreshAnlzCacheEntry(stableId, deckHolders[0].anlz);
		cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		await refreshSettled(cache, stableId);
		assert.equal(anlzRequestLog.length, 1, 'exactly one cache-bypassing /anlz request');
		assert.match(anlzRequestLog[0].url, /\/anlz\?/);
		const entry = cache.getAnlzEntry(stableId);
		assert.equal(entry.status, 'ready');
		assert.equal(entry.data.vocals.status, 'demucs');
		assert.equal(deckHolders[0].anlz.vocals.status, 'demucs');
		assert.equal(deckHolders[1].anlz.vocals.status, 'demucs');
		assert.equal(deckHolders[0].anlz.vocals.regions.length, 1);
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
	}
});

test('refreshAnlzVocalsIfNeeded skips fetch when observed status matches the cache', async () => {
	const stableId = 'unchanged-vocals-track';
	const vocals = { status: 'demucs', fps: 10, regions: [{ start_s: 0, end_s: 1, intensity: 0.5 }] };
	anlzCatalog.set(stableId, { vocals });
	anlzRequestLog.length = 0;
	const cache = await freshCache();
	try {
		cache.refreshAnlzCacheEntry(stableId, anlzPayload(vocals, stableId));
		cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		await new Promise((resolve) => setTimeout(resolve, 20));
		assert.equal(anlzRequestLog.length, 0, 'equal status must not issue a bypass fetch');
	} finally {
		anlzCatalog.delete(stableId);
	}
});

test('concurrent duplicate observations coalesce to one bypass fetch', async () => {
	const stableId = 'coalesce-vocals-track';
	const demucsVocals = {
		status: 'demucs',
		fps: 10,
		regions: [{ start_s: 0.5, end_s: 1.5, intensity: 0.6 }]
	};
	anlzCatalog.set(stableId, { vocals: demucsVocals });
	anlzRequestLog.length = 0;
	const cache = await freshCache();
	try {
		cache.refreshAnlzCacheEntry(stableId, anlzPayload({ status: 'not_analyzed' }, stableId));
		cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		await refreshSettled(cache, stableId);
		assert.equal(anlzRequestLog.length, 1, 'duplicate in-flight observations must coalesce');
	} finally {
		anlzCatalog.delete(stableId);
	}
});

test('a deck swap during refresh cannot graft vocals onto a different stable id', async () => {
	const stableId = 'swap-vocals-track';
	const deck = {
		stable_id: stableId,
		anlz: anlzPayload({ status: 'not_analyzed' }, stableId),
		loadToken: 0
	};
	deckHolders = [deck];
	const demucsVocals = {
		status: 'demucs',
		fps: 10,
		regions: [{ start_s: 2, end_s: 3, intensity: 0.7 }]
	};
	anlzCatalog.set(stableId, { vocals: demucsVocals });
	anlzRequestLog.length = 0;
	const cache = await freshCache();
	try {
		cache.refreshAnlzCacheEntry(stableId, deck.anlz);
		cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		deck.stable_id = 'other-track';
		deck.anlz = anlzPayload({ status: 'not_analyzed' }, 'other-track');
		deck.loadToken += 1;
		await refreshSettled(cache, stableId);
		assert.equal(anlzRequestLog.length, 1);
		assert.equal(deck.anlz.vocals.status, 'not_analyzed', 'swapped deck must keep its own vocals');
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
	}
});

test('an unload during refresh cannot graft vocals onto an empty deck', async () => {
	const stableId = 'unload-vocals-track';
	const deck = {
		stable_id: stableId,
		anlz: anlzPayload({ status: 'not_analyzed' }, stableId),
		loadToken: 0
	};
	deckHolders = [deck];
	const demucsVocals = {
		status: 'demucs',
		fps: 10,
		regions: [{ start_s: 1, end_s: 2, intensity: 0.4 }]
	};
	anlzCatalog.set(stableId, { vocals: demucsVocals });
	anlzRequestLog.length = 0;
	const cache = await freshCache();
	try {
		cache.refreshAnlzCacheEntry(stableId, deck.anlz);
		cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		deck.stable_id = null;
		deck.anlz = null;
		deck.loadToken += 1;
		await refreshSettled(cache, stableId);
		assert.equal(anlzRequestLog.length, 1);
		assert.equal(deck.anlz, null, 'unloaded deck must stay empty');
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
	}
});

test('a reload of the same stable id with newer vocals keeps its own payload', async () => {
	const stableId = 'reload-vocals-track';
	const deck = {
		stable_id: stableId,
		anlz: anlzPayload({ status: 'not_analyzed' }, stableId),
		loadToken: 0
	};
	deckHolders = [deck];
	const demucsVocals = {
		status: 'demucs',
		fps: 10,
		regions: [{ start_s: 3, end_s: 4, intensity: 0.9 }]
	};
	const newerVocals = {
		status: 'demucs',
		fps: 10,
		regions: [{ start_s: 3, end_s: 5, intensity: 0.95 }]
	};
	anlzCatalog.set(stableId, { vocals: demucsVocals });
	anlzRequestLog.length = 0;
	const cache = await freshCache();
	try {
		cache.refreshAnlzCacheEntry(stableId, deck.anlz);
		cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		deck.loadToken += 1;
		deck.anlz = anlzPayload(newerVocals, stableId);
		await refreshSettled(cache, stableId);
		assert.equal(anlzRequestLog.length, 1);
		assert.equal(deck.anlz.vocals.regions[0].end_s, 5, 'reloaded deck must keep its newer payload');
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
	}
});

test('a deck that loads the same track from the old cache mid-refresh adopts the refresh', async () => {
	const stableId = 'late-load-vocals-track';
	const oldAnlz = anlzPayload({ status: 'not_analyzed' }, stableId);
	const early = { stable_id: stableId, anlz: oldAnlz, loadToken: 0 };
	const late = { stable_id: null, anlz: null, loadToken: 0 };
	deckHolders = [early, late];
	const demucsVocals = {
		status: 'demucs',
		fps: 10,
		regions: [{ start_s: 4, end_s: 6, intensity: 0.5 }]
	};
	anlzCatalog.set(stableId, { vocals: demucsVocals });
	anlzRequestLog.length = 0;
	const cache = await freshCache();
	try {
		cache.refreshAnlzCacheEntry(stableId, oldAnlz);
		cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		late.stable_id = stableId;
		late.anlz = cache.getAnlzEntry(stableId).data;
		late.loadToken += 1;
		await refreshSettled(cache, stableId);
		assert.equal(anlzRequestLog.length, 1);
		assert.equal(early.anlz.vocals.status, 'demucs');
		assert.equal(
			late.anlz.vocals.status,
			'demucs',
			'a holder still on the old vocals must not stay stale'
		);
		cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		await new Promise((resolve) => setTimeout(resolve, 20));
		assert.equal(anlzRequestLog.length, 1, 'the cache now agrees, so no second fetch');
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
	}
});

test('a cache refresh by another request mid-flight still reaches decks holding the old vocals', async () => {
	const stableId = 'raced-vocals-track';
	const deck = {
		stable_id: stableId,
		anlz: anlzPayload({ status: 'not_analyzed' }, stableId),
		loadToken: 0
	};
	deckHolders = [deck];
	const demucsVocals = {
		status: 'demucs',
		fps: 10,
		regions: [{ start_s: 5, end_s: 6, intensity: 0.6 }]
	};
	anlzCatalog.set(stableId, { vocals: demucsVocals });
	anlzRequestLog.length = 0;
	const cache = await freshCache();
	try {
		cache.refreshAnlzCacheEntry(stableId, deck.anlz);
		cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs');
		// A different request (a duplicate load's revalidate) lands first.
		cache.refreshAnlzCacheEntry(stableId, anlzPayload(demucsVocals, stableId));
		const deadline = Date.now() + 5000;
		while (anlzRequestLog.length < 1 || deck.anlz.vocals.status !== 'demucs') {
			assert.ok(Date.now() < deadline, 'deck never adopted the vocals the other request cached');
			await new Promise((resolve) => setTimeout(resolve, 5));
		}
		assert.equal(deck.anlz.vocals.regions[0].start_s, 5);
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
	}
});

test('the vocals sink installs once and its uninstall frees the slot for a hot update', async () => {
	const cache = await freshCache();
	const sink = { snapshot: () => {}, publish: () => {} };
	assert.throws(() => cache.installAuthoritativeAnlzVocalsSink(sink), /already installed/);
	const fresh = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache.svelte.ts', {
		viteApiBase: origin
	});
	const release = fresh.installAuthoritativeAnlzVocalsSink(sink);
	release();
	fresh.installAuthoritativeAnlzVocalsSink(sink)();
});

test('a valid newer vocals payload from the bypass fetch republishes to the cache and every holder', async () => {
	// Pins that a validated newer payload is the one thing the bypass refresh republishes.
	const stableId = 'valid-newer-vocals-track';
	const rekordboxVocals = {
		status: 'rekordbox',
		fps: 150,
		regions: [
			{ start_s: 0.5, end_s: 4, intensity: 3 },
			{ start_s: 8, end_s: 9.25, intensity: 1 }
		]
	};
	anlzCatalog.set(stableId, { vocals: rekordboxVocals });
	anlzRequestLog.length = 0;
	try {
		const { cache } = await seededTwoDecks(stableId);
		cache.refreshAnlzVocalsIfNeeded(stableId, 'rekordbox');
		await refreshSettled(cache, stableId, 'rekordbox');
		assert.equal(anlzRequestLog.length, 1, 'exactly one cache-bypassing /anlz request');
		assert.deepEqual(cache.getAnlzEntry(stableId).data.vocals, rekordboxVocals);
		for (const deck of deckHolders) assert.deepEqual(deck.anlz.vocals, rekordboxVocals);
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
	}
});

const MALFORMED_CASES = [
	{
		name: 'an unknown vocals status',
		vocals: { status: 'whisper', fps: 10, regions: [] },
		error: /unknown status "whisper"/
	},
	{
		name: 'a region missing its intensity',
		vocals: { status: 'demucs', fps: 10, regions: [{ start_s: 1, end_s: 2 }] },
		error: /region 0 malformed/
	},
	{
		name: 'a rekordbox status with zero regions',
		vocals: { status: 'rekordbox', fps: 150, regions: [] },
		error: /rekordbox with zero regions/
	},
	{
		name: 'no vocals field at all',
		vocals: undefined,
		error: /no valid "vocals" field/
	}
];

for (const { name, vocals, error } of MALFORMED_CASES) {
	test(`a bypass response with ${name} fails loudly and preserves cache and deck state`, async () => {
		// Pins that a malformed vocals answer is refused before it can reach the cache or a deck.
		const stableId = `malformed-vocals-${name.replace(/\W+/g, '-')}`;
		anlzCatalog.set(stableId, { vocals });
		anlzRequestLog.length = 0;
		try {
			const { cache, seeded, before: beforeDecks } = await seededTwoDecks(stableId);
			const reason = await loudFailureOf(() => cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs'));
			assert.match(String(reason?.message ?? reason), error);
			assert.equal(anlzRequestLog.length, 1, 'the refresh did reach the server');
			assert.match(anlzRequestLog[0].url, /\/anlz\?/);
			assertStatePreserved(cache, stableId, seeded, beforeDecks);
			await assertRecoversWithValidPayload(cache, stableId);
		} finally {
			deckHolders = [];
			anlzCatalog.delete(stableId);
		}
	});
}

test('a bypass request the server answers with HTTP 500 fails loudly and preserves state', async () => {
	// Pins that an explicit server error on the bypass fetch republishes nothing.
	const stableId = 'http500-vocals-track';
	anlzCatalog.set(stableId, { fail: 'http500' });
	anlzRequestLog.length = 0;
	try {
		const { cache, seeded, before: beforeDecks } = await seededTwoDecks(stableId);
		const reason = await loudFailureOf(() => cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs'));
		assert.ok(reason instanceof Error, 'the failure surfaces as a real Error');
		assert.equal(reason.code, 'INTERNAL_ERROR');
		assert.equal(anlzRequestLog.length, 1);
		assertStatePreserved(cache, stableId, seeded, beforeDecks);
		await assertRecoversWithValidPayload(cache, stableId);
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
	}
});

test('a bypass request whose connection drops fails loudly and preserves state', async () => {
	// Pins that a network failure on the bypass fetch leaves the validated state in place.
	const stableId = 'socket-drop-vocals-track';
	anlzCatalog.set(stableId, { fail: 'socket' });
	anlzRequestLog.length = 0;
	try {
		const { cache, seeded, before: beforeDecks } = await seededTwoDecks(stableId);
		const reason = await loudFailureOf(() => cache.refreshAnlzVocalsIfNeeded(stableId, 'demucs'));
		assert.ok(reason instanceof Error, 'the failure surfaces as a real Error');
		assert.equal(anlzRequestLog.length, 1);
		assertStatePreserved(cache, stableId, seeded, beforeDecks);
		await assertRecoversWithValidPayload(cache, stableId);
	} finally {
		deckHolders = [];
		anlzCatalog.delete(stableId);
	}
});
