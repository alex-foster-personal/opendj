import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://usb-tracker-vendor.example.test';

let api;
let originalFetch;
let originalLocalStorage;

function jsonResponse(body) {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

before(async () => {
	api = await loadTypeScriptModule('src/lib/rb/usb-tracker.svelte.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
	originalLocalStorage = globalThis.localStorage;
	const store = new Map();
	globalThis.localStorage = {
		getItem: (key) => (store.has(key) ? store.get(key) : null),
		setItem: (key, value) => {
			store.set(key, String(value));
		},
		removeItem: (key) => {
			store.delete(key);
		},
		clear: () => store.clear()
	};
	api.usbTracker.volumes = [];
});

after(() => {
	globalThis.fetch = originalFetch;
	globalThis.localStorage = originalLocalStorage;
});

test('vendor-known stick skips first-seen prompt', async () => {
	globalThis.fetch = async () =>
		jsonResponse({
			volumes: [
				{
					id: 'vol:AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE',
					name: 'Gig Stick',
					mount_path: '/Volumes/Gig',
					kind: 'music',
					present: true,
					simulated: false,
					is_music: true,
					role: 'usb_stick',
					vendor_name: 'Gig Stick',
					vendor_source: 'rekordbox'
				}
			],
			scanned_at: 1,
			watching: false
		});

	await api.refreshUsbVolumes();
	const row = api.usbTracker.volumes.find((v) => v.id.startsWith('vol:'));
	assert.ok(row);
	assert.equal(row.name, 'Gig Stick');
	assert.equal(row.needs_prompt, false);
	assert.equal(api.usbTracker.promptId, null);
});

test('user rename in localStorage outranks a later vendor name', async () => {
	const id = 'vol:BBBBBBBB-CCCC-DDDD-EEEE-FFFFFFFFFFFF';
	const saved = [
		{
			id,
			name: 'My Custom Name',
			mount_path: '/Volumes/OLD',
			first_seen: 1,
			last_seen: 2,
			yours: true,
			is_music: true,
			present: true,
			needs_prompt: false
		}
	];
	globalThis.localStorage.setItem('mdt.rb.usb-volumes.v1', JSON.stringify(saved));
	api.usbTracker.volumes = saved.map((v) => ({ ...v }));

	globalThis.fetch = async () =>
		jsonResponse({
			volumes: [
				{
					id,
					name: 'Vendor Override',
					mount_path: '/Volumes/NEW',
					kind: 'music',
					present: true,
					simulated: false,
					is_music: true,
					role: 'usb_stick',
					vendor_name: 'Vendor Override',
					vendor_source: 'rekordbox'
				}
			],
			scanned_at: 2,
			watching: false
		});

	await api.refreshUsbVolumes();
	const row = api.usbTracker.volumes.find((v) => v.id === id);
	assert.equal(row?.name, 'My Custom Name');
});
