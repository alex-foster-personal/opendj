/**
 * PAIR-04 / PRRT_kwDOSEvNd86mL5Xn: when the reference master changes from A to
 * B while A's pairing request is in flight, A's slower answer must not
 * overwrite B's partners (the purple row underline would mark the wrong set).
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://pairing-index.example.test';

let mod;
let originalFetch;

function jsonResponse(body) {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

function pairing(id, from, to) {
	return {
		pairing_id: id,
		from_stable_id: from,
		to_stable_id: to,
		direction: '<->',
		source: 'manual',
		notes: null,
		snapshot: null,
		created_at: '2026-09-30T00:00:00+00:00',
		updated_at: '2026-09-30T00:00:00+00:00'
	};
}

/** Serve each master's pairings; a master listed in `held` waits for release(). */
function installPairingsServer(byMaster, held = new Set()) {
	const gates = new Map();
	for (const sid of held) {
		let release;
		const promise = new Promise((resolve) => {
			release = resolve;
		});
		gates.set(sid, { promise, release });
	}
	globalThis.fetch = async (request) => {
		const url = new URL(request.url);
		const from = url.searchParams.get('from_stable_id');
		const to = url.searchParams.get('to_stable_id');
		const sid = from ?? to;
		const gate = gates.get(sid);
		if (gate) await gate.promise;
		const rows = (byMaster[sid] ?? []).filter((p) =>
			from ? p.from_stable_id === from : p.to_stable_id === to
		);
		return jsonResponse(rows);
	};
	return (sid) => gates.get(sid).release();
}

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/pairing-index.svelte.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

const BY_MASTER = {
	'track-a': [pairing('p-a', 'track-a', 'partner-of-a')],
	'track-b': [pairing('p-b', 'partner-of-b', 'track-b')]
};

test('a slower response for the previous master does not overwrite the current partners', async () => {
	const release = installPairingsServer(BY_MASTER, new Set(['track-a']));
	const index = new mod.PairingIndex();

	const slowA = index.refresh(() => 'track-a');
	await index.refresh(() => 'track-b');
	assert.deepEqual([...index.partnerIds], ['partner-of-b']);

	release('track-a');
	await slowA;
	assert.deepEqual([...index.partnerIds], ['partner-of-b']);
});

test('control: a single in-flight refresh still publishes its partners', async () => {
	const release = installPairingsServer(BY_MASTER, new Set(['track-a']));
	const index = new mod.PairingIndex();

	const pending = index.refresh(() => 'track-a');
	assert.deepEqual([...index.partnerIds], []);
	release('track-a');
	await pending;
	assert.deepEqual([...index.partnerIds], ['partner-of-a']);
});

test('a response that lands after stop() does not repopulate the partners', async () => {
	const release = installPairingsServer(BY_MASTER, new Set(['track-a']));
	const index = new mod.PairingIndex();

	const pending = index.refresh(() => 'track-a');
	index.stop();
	release('track-a');
	await pending;
	assert.deepEqual([...index.partnerIds], []);
});
