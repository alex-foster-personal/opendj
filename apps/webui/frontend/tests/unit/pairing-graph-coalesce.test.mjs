/**
 * Sol P1 on PR #6062: one pairing write reached PairingGraph twice (the tab's own
 * announcement and the engine's pairings event), and each issued a full-list GET.
 * requestRefresh() folds notices that land within the window into one reload.
 *
 * The control: a notice after the window still reloads, so the debounce never
 * swallows a real later change.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://pairing-graph-coalesce.example.test';

let mod;
let originalFetch;
let listCalls;

before(async () => {
	originalFetch = globalThis.fetch;
	globalThis.fetch = async (request) => {
		const url = new URL(request.url);
		if (url.pathname === '/api/v1/pairings') {
			listCalls += 1;
			return new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } });
		}
		return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
	};
	mod = await loadTypeScriptModule('src/lib/rb/pairing-graph.svelte.ts', { viteApiBase: API_BASE });
});

after(() => {
	globalThis.fetch = originalFetch;
});

const settle = () => new Promise((resolve) => setTimeout(resolve, 300));

test('two change notices in one write issue one list GET', async () => {
	listCalls = 0;
	const graph = new mod.PairingGraph();
	graph.requestRefresh();
	graph.requestRefresh();
	await settle();
	assert.equal(listCalls, 1, 'the two notices coalesce into one GET');
});

test('control: a change after the window still reloads', async () => {
	listCalls = 0;
	const graph = new mod.PairingGraph();
	graph.requestRefresh();
	await settle();
	graph.requestRefresh();
	await settle();
	assert.equal(listCalls, 2, 'each separate change reloads');
});

test('a load answered after a change is queued never publishes its stale partners', async () => {
	const stubbed = globalThis.fetch;
	let release;
	const held = new Promise((resolve) => {
		release = resolve;
	});
	let calls = 0;
	const row = {
		pairing_id: 'p1',
		from_stable_id: 'a',
		to_stable_id: 'b',
		direction: '<->',
		source: 'manual',
		notes: null,
		snapshot: null,
		created_at: '2026-09-30T00:00:00+00:00',
		updated_at: '2026-09-30T00:00:00+00:00'
	};
	globalThis.fetch = async (request) => {
		const url = new URL(request.url);
		if (url.pathname !== '/api/v1/pairings') return new Response('{}', { status: 200 });
		calls += 1;
		if (calls === 1) await held;
		const body = calls === 1 ? [row] : [];
		return new Response(JSON.stringify(body), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	try {
		const graph = new mod.PairingGraph();
		const inFlight = graph.refresh();
		graph.requestRefresh();
		release();
		await inFlight;
		await new Promise((resolve) => setTimeout(resolve, 50));
		assert.equal(graph.partners.size, 0, 'the pre-change answer does not publish before the debounced reload');
		await settle();
	} finally {
		globalThis.fetch = stubbed;
	}
});
