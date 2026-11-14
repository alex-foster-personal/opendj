// requirement: SAMPLER-14629
/**
 * Issue #9987 (with #9994's server half, PR #14057): the classic sampler banks client builds the
 * GET and PUT /api/v1/sampler/banks requests and throws on any non-2xx.
 *
 * Regression lines:
 * - if the banks are read or saved then the URL, method, JSON body and headers match the route
 * - if the server answers 422 then the call throws with the status and the body
 *
 * Each test passes a fetch that records the request and replays a documented answer shape.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const BASE = 'https://daemon.example.test';
const URL_BANKS = `${BASE}/api/v1/sampler/banks`;

let api;
before(async () => {
	api = await loadTypeScriptModule('src/lib/player/sampler/api-sampler-banks.ts', {
		viteApiBase: BASE
	});
});

const view = () => ({
	v: 1,
	active_bank: 0,
	banks: Array.from({ length: 4 }, () => Array(16).fill(null))
});

function recorder(status, body) {
	const calls = [];
	const fetchFn = async (url, init = {}) => {
		calls.push({ url, method: init.method, headers: init.headers, body: init.body });
		return new Response(typeof body === 'string' ? body : JSON.stringify(body), { status });
	};
	return { calls, fetchFn };
}

test('GET /banks reads the document', async () => {
	/** [if] the banks are fetched [then] one GET to /api/v1/sampler/banks with no body and the view back, [else stop]. */
	const { calls, fetchFn } = recorder(200, view());
	const got = await api.fetchSamplerBanks(fetchFn);
	assert.deepEqual(calls, [
		{ url: URL_BANKS, method: 'GET', headers: { Accept: 'application/json' }, body: undefined }
	]);
	assert.equal(got.banks.length, 4);
});

test('PUT /banks sends the whole document as JSON', async () => {
	/** [if] a document is saved [then] one PUT with a JSON content type and exactly that document as body, [else stop]. */
	const doc = view();
	doc.banks[0][0] = { path: '/a.wav', mode: 'one_shot', gain_db: 0, title: 'a' };
	const answer = view();
	answer.banks[0][0] = { ...doc.banks[0][0], missing: false };
	const { calls, fetchFn } = recorder(200, answer);
	const got = await api.putSamplerBanks(doc, fetchFn);
	assert.equal(calls.length, 1);
	assert.equal(calls[0].url, URL_BANKS);
	assert.equal(calls[0].method, 'PUT');
	assert.deepEqual(calls[0].headers, {
		Accept: 'application/json',
		'Content-Type': 'application/json'
	});
	assert.deepEqual(JSON.parse(calls[0].body), doc);
	assert.equal(got.banks[0][0].missing, false);
});

test('a 422 throws with the status and the server body', async () => {
	/** [if] the server refuses a bad gain with 422 [then] the call rejects naming 422 and the detail, [else stop]. */
	const { fetchFn } = recorder(422, '{"detail":"gain_db 9 is outside -24..6 dB"}');
	await assert.rejects(api.putSamplerBanks(view(), fetchFn), (e) => {
		assert.equal(e.status, 422);
		assert.match(e.message, /save banks failed: HTTP 422 .*gain_db 9/);
		return true;
	});
});
