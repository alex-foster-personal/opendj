// requirement: EDGESAMP-13977
/**
 * Issue #10778 (S1): the edge sampler API client builds the requests the server routes take
 * (apps/webui/server/routes/sampler.py) and reads their answers, throwing on any non-2xx.
 *
 * Regression lines:
 * - if a call is made then the URL, method, JSON body and headers match the route
 * - if the server answers non-2xx then the call throws with the status and body
 * - if GET /sets or /pins lacks its array then the call throws instead of answering empty
 * - if a clip's X-Guard-Ms header is not 20 then fetchClipBytes throws
 *
 * Each test passes its own fetch function that records the request; no route answer is invented
 * beyond the shape a request test needs. Runtime against the real routes needs chain B merged.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const BASE = 'https://daemon.example.test';
const ROOT = `${BASE}/api/v1/sampler`;

let api;
before(async () => {
	api = await loadTypeScriptModule('src/lib/player/sampler/api-sampler.ts', { viteApiBase: BASE });
});

/** A fetch that records each call and answers `status` with `body` (JSON unless a string). */
function recorder(status = 200, body = {}, headers = {}) {
	const calls = [];
	const fetchFn = async (url, init = {}) => {
		calls.push({ url, method: init.method ?? 'GET', headers: init.headers, body: init.body });
		const text = typeof body === 'string' ? body : JSON.stringify(body);
		return new Response(status === 204 ? null : text, { status, headers });
	};
	return { calls, fetchFn };
}

test('GET /slots carries sort, q, offset, limit and the optional master', async () => {
	/** [if] slots are listed with and without a master [then] the query holds exactly those params, [else stop]. */
	const page = { slots: [], total: 0, skipped_no_grid: 1, rekordbox: 'ok' };
	const { calls, fetchFn } = recorder(200, page);
	const q = { sort: 'compat', q: 'acid house', offset: 40, limit: 20 };
	assert.deepEqual(await api.fetchSamplerSlots(q, fetchFn), page);
	await api.fetchSamplerSlots({ ...q, master_bpm: 124.5, master_key: '8A' }, fetchFn);
	assert.equal(calls[0].url, `${ROOT}/slots?sort=compat&q=acid+house&offset=40&limit=20`);
	assert.equal(
		calls[1].url,
		`${ROOT}/slots?sort=compat&q=acid+house&offset=40&limit=20&master_bpm=124.5&master_key=8A`
	);
	assert.equal(calls[0].method, 'GET');
	assert.equal(calls[0].body, undefined);
	assert.deepEqual(calls[0].headers, { Accept: 'application/json' });
});

test('slot get, patch, capture and delete hit /slots with the right method and body', async () => {
	/** [if] a slot is read, patched, captured and deleted [then] each goes to its route with its JSON body, [else stop]. */
	const { calls, fetchFn } = recorder(200, { slot_id: 's/1' });
	await api.fetchSamplerSlot('s/1', fetchFn);
	const settings = {
		launch: 'bar',
		key: 'auto',
		tempo: 'match',
		mode: 'loop',
		repeat: 4,
		downbeat_offset: 0,
		level: 80
	};
	await api.patchSamplerSlot('s/1', { name: null, settings }, fetchFn);
	const vector = {
		trim_db: 0,
		eq_db: { low: 0, mid: 0, high: 0 },
		filter: 0,
		stems: null,
		key_shift_st: 0,
		master_tempo: false
	};
	const capture = {
		stable_id: 't1',
		in_ms: 1000,
		out_ms: 3000,
		beats: 4,
		vector,
		set_id: 'set-a',
		index: 2
	};
	await api.postSamplerCapture(capture, fetchFn);
	const del = recorder(204);
	assert.equal(await api.deleteSamplerSlot('s/1', del.fetchFn), undefined);
	assert.deepEqual(
		[...calls, ...del.calls].map((c) => [c.method, c.url]),
		[
			['GET', `${ROOT}/slots/s%2F1`],
			['PATCH', `${ROOT}/slots/s%2F1`],
			['POST', `${ROOT}/slots`],
			['DELETE', `${ROOT}/slots/s%2F1`]
		]
	);
	assert.deepEqual(JSON.parse(calls[1].body), { name: null, settings });
	assert.deepEqual(JSON.parse(calls[2].body), capture);
	assert.equal(calls[1].headers['Content-Type'], 'application/json');
});

test('sets and pins unwrap their arrays and put their bodies', async () => {
	/** [if] sets and pins are read and written [then] the arrays come back and bodies match the models, [else stop]. */
	const sets = [{ id: 'set-a', name: 'A', description: '', slot_ids: ['s1', null] }];
	assert.deepEqual(await api.fetchSamplerSets(recorder(200, { sets }).fetchFn), sets);
	assert.deepEqual(await api.fetchSamplerPins(recorder(200, { pins: ['s1', 's2'] }).fetchFn), [
		's1',
		's2'
	]);
	const put = recorder(200, sets[0]);
	await api.putSamplerSet('set a', { name: 'A', description: '', slot_ids: null }, put.fetchFn);
	assert.equal(put.calls[0].url, `${ROOT}/sets/set%20a`);
	assert.deepEqual(JSON.parse(put.calls[0].body), { name: 'A', description: '', slot_ids: null });
	const pin = recorder(200, { pins: ['s1'] });
	assert.deepEqual(await api.putSamplerPin('s1', true, pin.fetchFn), ['s1']);
	assert.equal(pin.calls[0].method, 'PUT');
	assert.equal(pin.calls[0].url, `${ROOT}/pins/s1`);
	assert.deepEqual(JSON.parse(pin.calls[0].body), { pinned: true });
});

test('a list answer without its array throws', async () => {
	/** [if] GET /sets, GET /pins or PUT /pins answers without its array [then] the call throws, [else stop]. */
	await assert.rejects(
		api.fetchSamplerSets(recorder(200, []).fetchFn),
		/GET \/sets has no sets array/
	);
	await assert.rejects(
		api.fetchSamplerPins(recorder(200, {}).fetchFn),
		/GET \/pins has no pins array/
	);
	await assert.rejects(
		api.putSamplerPin('s1', false, recorder(200, {}).fetchFn),
		/PUT \/pins has no pins array/
	);
});

test('a non-2xx answer throws SamplerApiError with the status and body', async () => {
	/** [if] the server answers 409 NOT_A_CAPTURE [then] SamplerApiError carries 409 and the body, [else stop]. */
	const body = JSON.stringify({ detail: { code: 'NOT_A_CAPTURE', message: 'only captures' } });
	const err = await api.deleteSamplerSlot('s1', recorder(409, body).fetchFn).catch((e) => e);
	assert.ok(err instanceof api.SamplerApiError);
	assert.equal(err.status, 409);
	assert.equal(err.body, body);
	assert.match(err.message, /^sampler delete slot failed: HTTP 409 .*NOT_A_CAPTURE/);
});

test('a clip is fetched by slot and part and needs the 20 ms guard header', async () => {
	/** [if] a clip answers X-Guard-Ms 20 [then] its bytes come back, and any other value throws, [else stop]. */
	assert.equal(api.clipUrl('s1'), `${ROOT}/clips/s1`);
	assert.equal(api.clipUrl('s1', 'drums'), `${ROOT}/clips/s1?part=drums`);
	const ok = recorder(200, 'flac', { 'X-Guard-Ms': '20' });
	const bytes = await api.fetchClipBytes('s1', 'drums', ok.fetchFn);
	assert.equal(new TextDecoder().decode(bytes), 'flac');
	assert.equal(ok.calls[0].url, `${ROOT}/clips/s1?part=drums`);
	const bad = recorder(200, 'flac', { 'X-Guard-Ms': '0' });
	await assert.rejects(
		api.fetchClipBytes('s1', undefined, bad.fetchFn),
		/expected X-Guard-Ms 20, got 0/
	);
	await assert.rejects(
		api.fetchClipBytes('s1', undefined, recorder(404, 'gone').fetchFn),
		/HTTP 404 gone/
	);
});
