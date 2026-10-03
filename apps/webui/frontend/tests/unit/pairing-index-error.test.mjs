/**
 * PAIR-04 / Sol P1 on PR #4014 (r4161786229): a FAILED pairing lookup must not
 * read as a successful lookup with no pairings. PairingIndex keeps the two
 * apart (error state + one onError notification, which BrowserPanel turns into
 * an error toast) and still clears the underlines, which would be stale.
 *
 * Also Sol P2 (r4161786240): switching to a different master clears the
 * previous master's partners at once instead of after the new answer lands.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://pairing-index-error.example.test';

let mod;
let originalFetch;

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

/** mode: 'ok' serves `rows`; 'http500' returns a server error; 'malformed' a non-array body. */
function installServer(state) {
	globalThis.fetch = async (request) => {
		if (state.hold) await state.hold;
		const url = new URL(request.url);
		const from = url.searchParams.get('from_stable_id');
		const to = url.searchParams.get('to_stable_id');
		if (state.mode === 'http500') {
			return new Response(JSON.stringify({ detail: 'boom' }), {
				status: 500,
				headers: { 'content-type': 'application/json' }
			});
		}
		const body =
			state.mode === 'malformed'
				? { not: 'a list' }
				: state.rows.filter((p) => (from ? p.from_stable_id === from : p.to_stable_id === to));
		return new Response(JSON.stringify(body), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
}

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/pairing-index.svelte.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

for (const mode of ['http500', 'malformed']) {
	test(`a ${mode} lookup surfaces an error and is not an empty success`, async () => {
		const state = { mode: 'ok', rows: [pairing('p1', 'master', 'partner')] };
		installServer(state);
		const errors = [];
		const index = new mod.PairingIndex({ onError: (m) => errors.push(m) });

		await index.refresh(() => 'master');
		assert.deepEqual([...index.partnerIds], ['partner']);
		assert.equal(index.error, null);

		state.mode = mode;
		await index.refresh(() => 'master');
		assert.deepEqual([...index.partnerIds], [], 'stale partners must not stay underlined');
		assert.equal(typeof index.error, 'string', 'failure must be visible as an error state');
		assert.match(index.error, /pairing lookup failed/);
		assert.equal(errors.length, 1, 'the panel must be told once');
		assert.equal(errors[0], index.error);
	});
}

test('repeated failures notify once; a later success clears the error and re-arms it', async () => {
	const state = { mode: 'http500', rows: [pairing('p1', 'master', 'partner')] };
	installServer(state);
	const errors = [];
	const index = new mod.PairingIndex({ onError: (m) => errors.push(m) });

	await index.refresh(() => 'master');
	await index.refresh(() => 'master');
	assert.equal(errors.length, 1, 'a refresh storm must not spam toasts');

	state.mode = 'ok';
	await index.refresh(() => 'master');
	assert.equal(index.error, null);
	assert.deepEqual([...index.partnerIds], ['partner']);

	state.mode = 'http500';
	await index.refresh(() => 'master');
	assert.equal(errors.length, 2, 'a new failure after recovery notifies again');
});

test('control: a successful empty lookup is not an error', async () => {
	installServer({ mode: 'ok', rows: [] });
	const errors = [];
	const index = new mod.PairingIndex({ onError: (m) => errors.push(m) });
	await index.refresh(() => 'master');
	assert.deepEqual([...index.partnerIds], []);
	assert.equal(index.error, null);
	assert.equal(errors.length, 0);
});

test('a failure for a superseded master is dropped, not reported', async () => {
	let release;
	const state = {
		mode: 'http500',
		rows: [],
		hold: new Promise((r) => {
			release = r;
		})
	};
	installServer(state);
	const errors = [];
	const index = new mod.PairingIndex({ onError: (m) => errors.push(m) });
	const stale = index.refresh(() => 'track-a');
	index.stop();
	release();
	await stale;
	assert.equal(index.error, null);
	assert.equal(errors.length, 0);
});

test('switching master clears the previous partners before the new answer lands', async () => {
	let release;
	const state = {
		mode: 'ok',
		rows: [pairing('p-a', 'track-a', 'partner-of-a'), pairing('p-b', 'track-b', 'partner-of-b')]
	};
	installServer(state);
	const index = new mod.PairingIndex();
	await index.refresh(() => 'track-a');
	assert.deepEqual([...index.partnerIds], ['partner-of-a']);

	state.hold = new Promise((r) => {
		release = r;
	});
	const pendingB = index.refresh(() => 'track-b');
	assert.deepEqual([...index.partnerIds], [], "A's underlines must go as soon as B is the master");
	release();
	await pendingB;
	assert.deepEqual([...index.partnerIds], ['partner-of-b']);
});

test('control: a refresh for the SAME master keeps its partners while in flight', async () => {
	let release;
	const state = { mode: 'ok', rows: [pairing('p-a', 'track-a', 'partner-of-a')] };
	installServer(state);
	const index = new mod.PairingIndex();
	await index.refresh(() => 'track-a');
	state.hold = new Promise((r) => {
		release = r;
	});
	const again = index.bump(() => 'track-a');
	assert.deepEqual([...index.partnerIds], ['partner-of-a'], 'no flicker on a same-master reload');
	release();
	await again;
	assert.deepEqual([...index.partnerIds], ['partner-of-a']);
});
