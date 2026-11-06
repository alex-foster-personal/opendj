import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://pairing-graph-lifecycle.example.test';

let mod;
let originalFetch;

before(async () => {
	originalFetch = globalThis.fetch;
	mod = await loadTypeScriptModule('tests/unit/fixtures/pairing-graph-entry.ts', { viteApiBase: API_BASE });
});

afterEach(() => {
	globalThis.fetch = originalFetch;
	mod._resetPageGoingAwayForTests();
});

test('live page: pairing refresh failure sets error', async () => {
	globalThis.fetch = async (request) => {
		const url = new URL(request.url);
		if (url.pathname === '/api/v1/pairings') {
			throw new TypeError('Failed to fetch');
		}
		return new Response('{}', { status: 200 });
	};
	const graph = new mod.PairingGraph();
	await graph.refresh();
	assert.match(graph.error, /pairing list failed/);
});

test('going away: pairing refresh leaves error null', async () => {
	globalThis.fetch = async (request) => {
		const url = new URL(request.url);
		if (url.pathname === '/api/v1/pairings') {
			throw new TypeError('Failed to fetch');
		}
		return new Response('{}', { status: 200 });
	};
	mod.markPageGoingAwayForTests();
	const graph = new mod.PairingGraph();
	await graph.refresh();
	assert.equal(graph.error, null);
	assert.equal(graph.pending, false);
});
