import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://lifecycle-abort.example.test';

let mod;

async function loadMod() {
	mod = await loadTypeScriptModule('src/lib/api/lifecycle-abort.ts', { viteApiBase: API_BASE });
}

test('default: page is not going away', async () => {
	await loadMod();
	assert.equal(mod.isPageGoingAway(), false);
});

test('mark and reset test hooks flip the flag', async () => {
	await loadMod();
	mod.markPageGoingAwayForTests();
	assert.equal(mod.isPageGoingAway(), true);
	mod._resetPageGoingAwayForTests();
	assert.equal(mod.isPageGoingAway(), false);
});

test('installPageGoingAwayListener: pagehide and beforeunload set the flag', async () => {
	await loadMod();
	mod._resetPageGoingAwayForTests();
	const fakeWindow = new EventTarget();
	mod.installPageGoingAwayListener(fakeWindow);
	assert.equal(mod.isPageGoingAway(), false);
	fakeWindow.dispatchEvent(new Event('pagehide'));
	assert.equal(mod.isPageGoingAway(), true);
	mod._resetPageGoingAwayForTests();
	fakeWindow.dispatchEvent(new Event('beforeunload'));
	assert.equal(mod.isPageGoingAway(), true);
});

test('exceptions on a live page do not flip the flag', async () => {
	await loadMod();
	mod._resetPageGoingAwayForTests();
	void new TypeError('Failed to fetch');
	if (typeof DOMException !== 'undefined') void new DOMException('aborted', 'AbortError');
	assert.equal(mod.isPageGoingAway(), false);
});

afterEach(() => {
	if (mod?._resetPageGoingAwayForTests) mod._resetPageGoingAwayForTests();
});
