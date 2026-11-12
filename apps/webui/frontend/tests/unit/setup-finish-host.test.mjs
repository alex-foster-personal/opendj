/**
 * SETUP-12768 (#11423): finishing setup must not remount /performance while a
 * deck is playing.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { engineHealth, jsonResponse } from './setup-fixtures.mjs';

async function loadRunSetup() {
	return loadTypeScriptModule('src/lib/setup/run-setup.ts');
}

test('[if] Start playing on /performance with a live deck [then] host goto is skipped, [else stop].', async () => {
	const mod = await loadRunSetup();
	assert.equal(mod.shouldNavigateSetupDoor('/performance', true), false);
	const navigated = [];
	assert.equal(
		mod.navigateToSetupHostAfterClose((path, _opts) => navigated.push(path), '/performance', true),
		false
	);
	assert.deepEqual(navigated, []);
});

test('[if] Start playing on /performance with no playing deck [then] host goto is skipped, [else stop].', async () => {
	const mod = await loadRunSetup();
	assert.equal(mod.shouldNavigateSetupDoor('/performance', false), false);
});

test('[if] Start playing on the /setup door with no playing deck [then] host goto runs, [else stop].', async () => {
	const mod = await loadRunSetup();
	const navigated = [];
	assert.equal(
		mod.navigateToSetupHostAfterClose(
			(path, opts) => navigated.push({ path, opts }),
			'/setup',
			false
		),
		true
	);
	assert.deepEqual(navigated, [{ path: '/performance', opts: { replaceState: true } }]);
});

test('[if] runSetup is called from /performance [then] it does not visit /setup, [else stop].', async () => {
	const mod = await loadTypeScriptModule('tests/unit/fixtures/run-setup-entry.ts', {
		viteApiBase: 'https://finish-host.example.test'
	});
	const navigated = [];
	// Minimal fetch stub for reopen path.
	globalThis.fetch = async (request) => {
		const { pathname } = new URL(request.url);
		if (pathname === '/api/v1/health') return jsonResponse(engineHealth());
		if (pathname === '/api/v1/setup/dismiss')
			return jsonResponse({
				library_empty: false,
				tracks: 1,
				playlists: 0,
				state_db: { path: 'd', exists: true, size_bytes: 1, modified_at: null },
				data_dir: '/data',
				dismissed: false,
				dev_mode: false,
				should_show_wizard: false,
				stages: [],
				folder_stages: [],
				last_import: null,
				rekordbox: null,
				permissions: null
			});
		throw new Error(`unexpected ${pathname}`);
	};
	assert.equal(await mod.runSetup((path) => navigated.push(path), '/performance'), null);
	assert.deepEqual(navigated, []);
});
