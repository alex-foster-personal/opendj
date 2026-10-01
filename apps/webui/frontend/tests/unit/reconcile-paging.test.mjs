/**
 * The Missing tracks page loads a page at a time and says what it has.
 *
 * It used to ask for every broken track in one request (7,127 rows, 3.9 MB,
 * 5 to 9 s on a 9,713-track library) behind a bare "Loading...", and a failed
 * load fell through to "No broken tracks. Library is clean.".
 *
 * Regression one-liners:
 *   - if listBrokenPage does not send limit and offset then broken
 *   - if the first load asks for more than one page then broken
 *   - if a reload after an edit shrinks what the user had loaded then broken
 *   - if a request asks for more rows than the route allows then broken
 *   - if the count line quotes loaded rows as the total then broken
 *   - if a failed load can be read as "library is clean" then broken
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://reconcile-paging.example.test';

let api;
let paging;
let originalFetch;

before(async () => {
	api = await loadTypeScriptModule('src/lib/reconcile-api.ts', { viteApiBase: API_BASE });
	paging = await loadTypeScriptModule('src/lib/reconcile-paging.ts');
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

/** A fake daemon listing of `total` broken rows, recording each request. */
function listing(total) {
	const requests = [];
	const fetchPage = async (limit, offset) => {
		requests.push({ limit, offset });
		const end = Math.min(offset + limit, total);
		const tracks = [];
		for (let i = offset; i < end; i += 1) tracks.push({ stable_id: `t${i}` });
		return { total, tracks, offset, next_offset: end < total ? end : null };
	};
	return { requests, fetchPage };
}

test('listBrokenPage sends limit and offset on the wire', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return new Response(JSON.stringify({ total: 0, tracks: [], offset: 400, next_offset: null }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	const page = await api.listBrokenPage(200, 400);

	assert.equal(seen.url, `${API_BASE}/api/v1/reconcile/broken?limit=200&offset=400`);
	assert.equal(seen.method, 'GET');
	assert.equal(page.next_offset, null);
});

test('the first load is one request for one page', async () => {
	const { requests, fetchPage } = listing(7127);

	const got = await paging.loadBrokenRows(fetchPage, paging.MISSING_PAGE_SIZE);

	assert.deepEqual(requests, [{ limit: 200, offset: 0 }]);
	assert.equal(got.tracks.length, 200);
	assert.equal(got.total, 7127);
	assert.equal(got.nextOffset, 200);
});

test('a reload keeps what was loaded, in requests no larger than the route allows', async () => {
	const { requests, fetchPage } = listing(7127);

	const got = await paging.loadBrokenRows(fetchPage, 2400);

	assert.deepEqual(requests, [
		{ limit: 1000, offset: 0 },
		{ limit: 1000, offset: 1000 },
		{ limit: 400, offset: 2000 }
	]);
	assert.equal(got.tracks.length, 2400);
	assert.deepEqual(got.tracks.slice(0, 2).map((t) => t.stable_id), ['t0', 't1']);
	assert.equal(got.tracks[2399].stable_id, 't2399');
	assert.equal(got.nextOffset, 2400);
});

test('loading stops at the end of the listing and reports no next page', async () => {
	const { requests, fetchPage } = listing(130);

	const got = await paging.loadBrokenRows(fetchPage, 200);

	assert.deepEqual(requests, [{ limit: 200, offset: 0 }]);
	assert.equal(got.tracks.length, 130);
	assert.equal(got.nextOffset, null);
});

test('an empty listing is zero rows, zero total, no next page', async () => {
	const { fetchPage } = listing(0);
	const got = await paging.loadBrokenRows(fetchPage, 200);
	assert.deepEqual(got, { tracks: [], total: 0, nextOffset: null });
});

test('loadBrokenRows refuses a non-positive want instead of looping', async () => {
	const { fetchPage } = listing(10);
	await assert.rejects(paging.loadBrokenRows(fetchPage, 0), /want must be a positive integer/);
});

test('a daemon that stops advancing is an error, not an endless load', async () => {
	const stuck = async (limit, offset) => ({
		total: 500, tracks: [{ stable_id: 'same' }], offset, next_offset: offset
	});
	await assert.rejects(paging.loadBrokenRows(stuck, 400), /did not advance/);
});

test('the count line names loaded rows against the whole count', () => {
	assert.equal(paging.missingCountLabel(200, 7127), 'Showing 200 of 7,127 missing tracks');
	assert.equal(paging.missingCountLabel(7127, 7127), '7,127 missing tracks');
	assert.equal(paging.missingCountLabel(1, 1), '1 missing track');
});

test('the count title says what is counted, when, and that it is not a share of the library', () => {
	const title = paging.missingCountTitle(7127, new Date(2026, 9, 1, 13, 5, 9));
	assert.match(title, /7,127/);
	assert.match(title, /recorded local path does not resolve on this machine/);
	assert.match(title, /13:05:09/);
	assert.match(title, /not a share of the library/i);
	assert.match(title, /drive that is not plugged in/);
});

test('the more button says how many rows it will add', () => {
	assert.equal(paging.missingMoreLabel(200, 7127), 'Show 200 more');
	assert.equal(paging.missingMoreLabel(7000, 7127), 'Show 127 more');
});

test('a failed load is its own state and never reads as an empty listing', () => {
	assert.equal(paging.missingView({ loading: true, error: null, loaded: 0 }), 'loading');
	assert.equal(paging.missingView({ loading: false, error: 'HTTP 500', loaded: 0 }), 'error');
	assert.equal(paging.missingView({ loading: false, error: null, loaded: 0 }), 'empty');
	// Rows already on screen stay on screen while more load, or after a later
	// request fails: the error is shown beside them, not in place of them.
	assert.equal(paging.missingView({ loading: true, error: null, loaded: 200 }), 'rows');
	assert.equal(paging.missingView({ loading: false, error: 'HTTP 500', loaded: 200 }), 'rows');
});
