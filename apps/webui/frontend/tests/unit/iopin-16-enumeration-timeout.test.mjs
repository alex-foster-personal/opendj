// requirement: IOPIN-5369 (hung device listing is timeout state, not a Sentry headphone-diagnostic)
// [if] enumerateDevices hangs past the operation timeout [then] refresh resolves as device_access timeout and does not record headphone-diagnostic at error
// [if] enumeration rejects with a real device error [then] refresh still throws headphone output enumeration failed
//
// Regression line: OPEN-DJ-BE-77 / OPEN-DJ-BE-74. IOPIN-14 already named the
// hung list `timeout` with a system-default fallback, then `_headphoneError`
// still recorded headphone-diagnostic at severity error, which always POSTs
// to Sentry. A missing audio device is not this fingerprint; a 5s hang of
// enumerateDevices or native list() is.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const HEADPHONES_SRC = readFileSync(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');

let headphones;
let playerState;

const savedNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');

function installNavigator({ enumerateDevices }) {
	const mediaDevices = {
		enumerateDevices,
		addEventListener() {},
		removeEventListener() {}
	};
	Object.defineProperty(globalThis, 'navigator', {
		value: { mediaDevices },
		configurable: true,
		writable: true
	});
}

function hp() {
	return playerState.mixerState.headphones;
}

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	playerState = await loadTypeScriptModule('src/lib/player/state.svelte.ts');
});

afterEach(() => {
	headphones.disposeHeadphoneMonitor();
	if (savedNavigator === undefined) delete globalThis.navigator;
	else Object.defineProperty(globalThis, 'navigator', savedNavigator);
	const state = hp();
	state.outputs = [];
	state.inputs = [];
	state.error = null;
});

test('if refreshHeadphoneOutputs catches a listing hang then it returns without _headphoneError', () => {
	const fn = HEADPHONES_SRC.slice(HEADPHONES_SRC.indexOf('export async function refreshHeadphoneOutputs'));
	const listingCatch = fn.slice(fn.indexOf('} catch (error) {'), fn.indexOf('mixerState.headphones.outputs = listing.outputs'));
	assert.match(listingCatch, /if \(ioOperationTimedOut\(error\)\)/);
	assert.match(listingCatch, /publishIoDeviceAccessFailure\('timeout'/);
	assert.match(listingCatch, /\breturn;/);
	assert.match(listingCatch, /throw _headphoneError\('headphone output enumeration failed'/);
	assert.ok(
		listingCatch.indexOf("if (ioOperationTimedOut(error))") <
			listingCatch.indexOf("throw _headphoneError('headphone output enumeration failed'"),
		'timeout must return before the Sentry-escalating wrap'
	);
});

test('if enumeration hangs then refresh resolves as timeout and does not log headphone-diagnostic at error', async (t) => {
	t.mock.timers.enable({ apis: ['setTimeout'] });
	installNavigator({ enumerateDevices: () => new Promise(() => {}) });
	const diagnosticErrors = [];
	const originalError = console.error;
	console.error = (...args) => {
		diagnosticErrors.push(args.map(String).join(' '));
		originalError.apply(console, args);
	};
	try {
		const pending = headphones.refreshHeadphoneOutputs();
		t.mock.timers.tick(60_000);
		await pending;
	} finally {
		console.error = originalError;
	}
	assert.equal(hp().device_access.status, 'timeout');
	assert.match(hp().error ?? '', /timed out/);
	assert.equal(
		diagnosticErrors.some((line) => line.includes('headphone-diagnostic')),
		false,
		'error-severity headphone-diagnostic would escalate to Sentry'
	);
});

test('if enumeration rejects then refresh still throws the headphone-diagnostic wrap', async () => {
	installNavigator({
		enumerateDevices: async () => {
			throw Object.assign(new Error('device hub offline'), { name: 'NotReadableError' });
		}
	});
	await assert.rejects(() => headphones.refreshHeadphoneOutputs(), /enumeration failed: device hub offline/);
	assert.equal(hp().device_access.status, 'enumeration_failed');
});
