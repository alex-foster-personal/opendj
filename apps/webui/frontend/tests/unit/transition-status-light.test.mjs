/**
 * TRANS-01: documented TransitionStatusLight adapter, no-device default.
 *
 * [if] no TransitionStatusLight is registered [then] the light control is
 *   inert and its tooltip contains `no device` and `not implemented`
 */
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const mod = await loadTypeScriptModule('src/lib/rb/transition-status-light.ts');
const unregisters = [];

afterEach(() => {
	while (unregisters.length > 0) {
		unregisters.pop()();
	}
});

test('default adapter id is none', () => {
	assert.equal(mod.getTransitionStatusLight().id, 'none');
});

test('default describe names no device and not implemented', () => {
	const text = mod.getTransitionStatusLight().describe();
	assert.match(text, /no device/i);
	assert.match(text, /not implemented/i);
});

test('default setState does not throw', () => {
	assert.doesNotThrow(() => mod.getTransitionStatusLight().setState('transitioning'));
	assert.doesNotThrow(() => mod.getTransitionStatusLight().setState('approaching'));
	assert.doesNotThrow(() => mod.getTransitionStatusLight().setState('idle'));
});

test('register swaps the adapter and unregister restores the default', () => {
	const recorded = [];
	const adapter = {
		id: 'test',
		describe: () => 'test light',
		setState(state) {
			recorded.push(state);
		}
	};
	unregisters.push(mod.registerTransitionStatusLight(adapter));
	assert.equal(mod.getTransitionStatusLight().id, 'test');
	mod.getTransitionStatusLight().setState('approaching');
	assert.deepEqual(recorded, ['approaching']);
	unregisters.pop()();
	assert.equal(mod.getTransitionStatusLight().id, 'none');
});
