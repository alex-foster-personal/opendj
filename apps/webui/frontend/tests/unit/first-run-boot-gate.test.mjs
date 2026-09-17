/**
 * First-run auto-open gate state (issue #2722 P0-1): visible error on timeout,
 * successful open after probe retries.
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { engineHealth, jsonResponse } from './setup-fixtures.mjs';

const API_BASE = 'https://first-run-boot-gate.example.test';

function status(overrides = {}) {
	return {
		library_empty: true,
		tracks: 0,
		playlists: 0,
		state_db: { path: '/data/state/state.db', exists: false, size_bytes: null, modified_at: null },
		data_dir: '/data',
		dismissed: false,
		dev_mode: false,
		should_show_wizard: true,
		stages: ['detect'],
		folder_stages: ['detect'],
		last_import: null,
		rekordbox: null,
		permissions: null,
		...overrides
	};
}

let mod;
let originalFetch;

before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/first-run-boot-gate-entry.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	globalThis.fetch = originalFetch;
	mod.capabilities._resetForTests();
	mod._resetFirstRunGateForTests();
});

test('runFirstRunGate returns true and clears resolving after a cold-start retry', async () => {
	let healthCalls = 0;
	globalThis.fetch = async (request) => {
		const path = new URL(request.url).pathname;
		if (path === '/api/v1/health') {
			healthCalls += 1;
			if (healthCalls < 2) throw new TypeError('ECONNREFUSED');
			return jsonResponse(engineHealth());
		}
		return jsonResponse(status());
	};
	assert.equal(mod.firstRunGate.phase, 'idle');
	const result = await mod.runFirstRunGate();
	assert.equal(result, true);
	assert.equal(mod.firstRunGate.phase, 'idle');
	assert.equal(mod.firstRunGate.hasError, false);
});

test('runFirstRunGate surfaces a visible error when the engine never answers', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('ECONNREFUSED');
	};
	const result = await mod.runFirstRunGate({ timeoutMs: 600 });
	assert.equal(result, null);
	assert.equal(mod.firstRunGate.hasError, true);
	assert.match(mod.firstRunGate.error, /could not reach the engine/i);
});
