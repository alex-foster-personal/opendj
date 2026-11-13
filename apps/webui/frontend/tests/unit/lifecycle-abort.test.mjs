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

test('beginSoftNavigationHandoff covers SPA navigations', async () => {
	await loadMod();
	mod._resetPageGoingAwayForTests();
	const end = mod.beginSoftNavigationHandoff();
	assert.equal(mod.isPageGoingAway(), true);
	end();
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

test('msSincePageGoingAway: null until a signal, then the age of the latest signal', async () => {
	await loadMod();
	mod._resetPageGoingAwayForTests();
	assert.equal(mod.msSincePageGoingAway(), null);
	const fakeWindow = new EventTarget();
	mod.installPageGoingAwayListener(fakeWindow);
	const realNow = Date.now;
	try {
		Date.now = () => 1_000;
		fakeWindow.dispatchEvent(new Event('beforeunload'));
		Date.now = () => 1_250;
		assert.equal(mod.msSincePageGoingAway(), 250);
		Date.now = () => 5_000;
		fakeWindow.dispatchEvent(new Event('pagehide'));
		Date.now = () => 5_040;
		assert.equal(mod.msSincePageGoingAway(), 40, 'the latest signal restarts the clock');
	} finally {
		Date.now = realNow;
	}
	mod._resetPageGoingAwayForTests();
	assert.equal(mod.msSincePageGoingAway(), null);
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
