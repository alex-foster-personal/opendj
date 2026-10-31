/**
 * Settings > "Open in browser" states (UIURL-03,
 * apps/webui/frontend/src/lib/settings/open-in-browser.ts).
 *
 * - if a Tauri or Electron page with an address does not render 'open' then
 *   the app has no way to hand the library to a browser -> broken
 * - if a browser tab renders 'open' then clicking it opens a second tab of
 *   the page already on screen -> broken (over-detection control)
 * - if a failed or pending address read renders an enabled control then a
 *   click opens nothing with no reason given -> broken
 * - if a share-tunnel page shows the engine's local address then a remote
 *   viewer is handed an address that cannot reach the engine -> broken
 * - if an address with userinfo or a lookalike host renders 'open' then the
 *   shell hands an attacker host to the browser -> broken
 * - if the open button's hover title does not say "same library" then the
 *   user cannot tell the tab and the window share one library -> broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/settings/open-in-browser.ts');
});

const ADDRESS = {
	ui_url: 'http://opendj.localhost:7700/',
	loopback_url: 'http://127.0.0.1:7700/',
	port: 7700,
	served_by: 'engine'
};

function view(overrides) {
	return mod.openInBrowserView({
		address: ADDRESS,
		error: null,
		shell: null,
		pageHostname: '127.0.0.1',
		...overrides
	});
}

test('a Tauri page with an address offers to open the named address', () => {
	const result = view({ shell: 'tauri' });
	assert.equal(result.kind, 'open');
	assert.equal(result.url, 'http://opendj.localhost:7700/');
});

test('an Electron page with an address offers to open it too', () => {
	assert.equal(view({ shell: 'electron' }).kind, 'open');
});

test('a browser tab on loopback shows the address to copy, not an open button', () => {
	const result = view({ shell: null, pageHostname: '127.0.0.1' });
	assert.equal(result.kind, 'copy');
	assert.equal(result.url, 'http://opendj.localhost:7700/');
});

test('a browser tab already on opendj.localhost still shows copy', () => {
	assert.equal(view({ shell: null, pageHostname: 'opendj.localhost' }).kind, 'copy');
});

test('a pending address read renders inert with a reason', () => {
	const result = view({ shell: 'tauri', address: null });
	assert.equal(result.kind, 'loading');
	assert.match(result.title, /browser address/);
});

test('a refused address read renders inert and names the refusal', () => {
	const result = view({ shell: 'tauri', address: null, error: 'UI_NOT_SERVED' });
	assert.equal(result.kind, 'unavailable');
	assert.match(result.title, /UI_NOT_SERVED/);
});

test('a page reached through a non-local host hides the control', () => {
	assert.equal(view({ shell: null, pageHostname: 'dj.example' }).kind, 'hidden');
	// Negative control: a lookalike of the named host is not local either.
	assert.equal(view({ shell: null, pageHostname: 'evil.localhost' }).kind, 'hidden');
});

test('the open button is labeled plainly and its title says same library', () => {
	assert.equal(mod.OPEN_IN_BROWSER_LABEL, 'Open in browser');
	const result = view({ shell: 'tauri' });
	assert.match(result.title, /same library/);
	assert.match(view({ shell: null }).title, /same engine and library/);
});

test('an address smuggling another host is refused, never opened', () => {
	for (const ui_url of [
		'http://opendj.localhost:7700@attacker.example/',
		'http://opendj.localhost.attacker.example:7700/',
		'https://opendj.localhost:7700/',
		'not a url'
	]) {
		const result = view({ shell: 'tauri', address: { ...ADDRESS, ui_url } });
		assert.equal(result.kind, 'unavailable', ui_url);
	}
	// Positive control: the real shape still opens.
	assert.equal(view({ shell: 'tauri' }).kind, 'open');
});
