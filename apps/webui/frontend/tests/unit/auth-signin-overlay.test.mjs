/**
 * Sign-in overlay store transitions and cancel wiring.
 */

import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

afterEach(async () => {
	const mod = await loadTypeScriptModule('src/lib/auth/sign-in-overlay.svelte.ts');
	mod._resetSignInOverlayForTests();
});

test('overlay opens on beginSignIn and returns idle after completeSignIn', async () => {
	const mod = await loadTypeScriptModule('src/lib/auth/sign-in-overlay.svelte.ts');
	assert.equal(mod.signInOverlay.open, false);
	mod.beginSignIn();
	assert.equal(mod.signInOverlay.open, true);
	assert.equal(mod.signInOverlay.phase, 'starting');
	mod.completeSignIn();
	assert.equal(mod.signInOverlay.open, false);
	assert.equal(mod.signInOverlay.phase, 'idle');
});

test('cancelSignIn invokes the registered abort and closes the overlay', async () => {
	const mod = await loadTypeScriptModule('src/lib/auth/sign-in-overlay.svelte.ts');
	let cancelled = false;
	mod.beginSignIn();
	mod.setSignInPhase('waiting');
	mod.registerSignInCancel(() => {
		cancelled = true;
	});
	mod.cancelSignIn();
	assert.equal(cancelled, true);
	assert.equal(mod.signInOverlay.open, false);
});

test('failSignIn clears a registered cancel handler', async () => {
	const mod = await loadTypeScriptModule('src/lib/auth/sign-in-overlay.svelte.ts');
	let cancelled = false;
	mod.beginSignIn();
	mod.registerSignInCancel(() => {
		cancelled = true;
	});
	mod.failSignIn();
	assert.equal(cancelled, true);
	assert.equal(mod.signInOverlay.open, false);
});
