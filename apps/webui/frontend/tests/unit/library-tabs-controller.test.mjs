import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://library-tabs-controller.example.test';

let mod;
let originalFetch;

before(async () => {
	originalFetch = globalThis.fetch;
	mod = await loadTypeScriptModule('tests/unit/fixtures/library-tabs-controller-entry.ts', {
		viteApiBase: API_BASE
	});
});

afterEach(() => {
	globalThis.fetch = originalFetch;
	mod._resetPageGoingAwayForTests();
});

test('restore reports on a live fetch failure', async () => {
	const reported = [];
	globalThis.fetch = async () => {
		throw new TypeError('Failed to fetch');
	};
	const ctrl = mod.createLibraryTabsController({
		panes: [],
		activePane: () => 0,
		setActivePane: () => {},
		newPane: () => ({}),
		isKnownPlaylist: () => false,
		nodeFor: () => null,
		loadPane: async () => {},
		report: (m) => reported.push(m),
		warn: () => {}
	});
	await ctrl.restore(true);
	assert.ok(reported.some((m) => /restoring library tabs failed/.test(m)));
});

test('restore stays quiet when the page is going away', async () => {
	const reported = [];
	globalThis.fetch = async () => {
		throw new TypeError('Failed to fetch');
	};
	mod.markPageGoingAwayForTests();
	const ctrl = mod.createLibraryTabsController({
		panes: [],
		activePane: () => 0,
		setActivePane: () => {},
		newPane: () => ({}),
		isKnownPlaylist: () => false,
		nodeFor: () => null,
		loadPane: async () => {},
		report: (m) => reported.push(m),
		warn: () => {}
	});
	await ctrl.restore(true);
	assert.equal(reported.length, 0);
});

test('restore still reports a live non-fetch restore failure', async () => {
	const reported = [];
	globalThis.fetch = async () =>
		new Response(JSON.stringify({}), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	const ctrl = mod.createLibraryTabsController({
		panes: [],
		activePane: () => 0,
		setActivePane: () => {},
		newPane: () => ({}),
		isKnownPlaylist: () => false,
		nodeFor: () => null,
		loadPane: async () => {},
		report: (m) => reported.push(m),
		warn: () => {}
	});
	await ctrl.restore(true);
	assert.ok(reported.some((m) => /library_tabs is missing/.test(m)));
});
