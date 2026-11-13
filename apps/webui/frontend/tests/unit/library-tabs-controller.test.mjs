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
	await ctrl.restore(() => true);
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
	await ctrl.restore(() => true);
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
	await ctrl.restore(() => true);
	assert.ok(reported.some((m) => /library_tabs is missing/.test(m)));
});

test('[if] the user opens a list while the saved tabs are still being read [then] restore leaves that choice alone [else stop]', async () => {
	// Main E2E run 43364041357: a playlist click landed during the boot read of
	// library_tabs, then the read's All Tracks tab was loaded over it.
	let userChose = false;
	globalThis.fetch = async () => {
		userChose = true;
		return new Response(
			JSON.stringify({
				library_tabs: {
					tabs: [
						{ playlist_id: 'all', name: 'All Tracks', search: '', sort_key: null, sort_dir: 1 }
					],
					active: 0
				}
			}),
			{ status: 200, headers: { 'content-type': 'application/json' } }
		);
	};
	const loaded = [];
	const pane = { playlist_id: null, setSearch: () => {} };
	const ctrl = mod.createLibraryTabsController({
		panes: [pane],
		activePane: () => 0,
		setActivePane: () => {},
		newPane: () => ({ playlist_id: null, setSearch: () => {} }),
		isKnownPlaylist: () => true,
		nodeFor: (id, name) => ({ playlist_id: id, name }),
		loadPane: async (_p, node) => {
			loaded.push(node.playlist_id);
		},
		report: () => {},
		warn: () => {}
	});
	const reopened = await ctrl.restore(() => !userChose);
	if (loaded.length > 0)
		console.log('if restore loads saved tabs over a choice made during its read then broken');
	assert.deepEqual(loaded, []);
	assert.equal(reopened, false);
});
