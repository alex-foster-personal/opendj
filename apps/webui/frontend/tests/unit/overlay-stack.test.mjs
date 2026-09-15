/**
 * Root overlay z-index stack and boot-gate yield invariants (issue #2722 P0-2).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let stack;
let preflightHelpers;

before(async () => {
	stack = await loadTypeScriptModule('src/lib/overlays/overlay-stack.ts');
	preflightHelpers = await loadTypeScriptModule('src/lib/preflight/preflight.svelte.ts');
});

test('bootGateYielded: any modal overlay lowers the blocking gate', () => {
	assert.equal(
		stack.bootGateYielded({ setup: false, settings: true, account: false, signIn: false }),
		true
	);
	assert.equal(
		stack.bootGateYielded({ setup: false, settings: false, account: true, signIn: false }),
		true
	);
	assert.equal(
		stack.bootGateYielded({ setup: false, settings: false, account: false, signIn: true }),
		true
	);
	assert.equal(
		stack.bootGateYielded({ setup: true, settings: false, account: false, signIn: false }),
		true
	);
	assert.equal(
		stack.bootGateYielded({ setup: false, settings: false, account: false, signIn: false }),
		false
	);
});

test('shouldBlockOnPreflight yields when settings is open on a failing gate', () => {
	assert.equal(preflightHelpers.shouldBlockOnPreflight('fail', true), false);
	assert.equal(preflightHelpers.shouldBlockOnPreflight('fail', false), true);
});

test('documented pairs place settings below the boot gate unless yielded', () => {
	for (const pair of stack.HIDDEN_BEHIND_PAIRS) {
		assert.ok(
			stack.OVERLAY_Z_INDEX[pair.over] > stack.OVERLAY_Z_INDEX[pair.under],
			`${pair.under} must paint below ${pair.over} without a yield`
		);
	}
});

test('highest open overlay wins when boot gate has yielded', () => {
	const open = ['preflightBoot', 'settings'];
	assert.equal(stack.highestOpenOverlayWins(open, false), false);
	assert.equal(stack.highestOpenOverlayWins(['settings'], false), true);
});

test('every boot-gate yield overlay is below preflightBoot in z-index', () => {
	for (const id of stack.BOOT_GATE_YIELD_OVERLAYS) {
		assert.ok(
			stack.OVERLAY_Z_INDEX[id] < stack.OVERLAY_Z_INDEX.preflightBoot,
			`${id} must yield because it sits under the boot gate`
		);
	}
});

test('overlaysByZIndex sorts highest first', () => {
	assert.deepEqual(stack.overlaysByZIndex(['settings', 'preflightBoot', 'account']), [
		'preflightBoot',
		'settings',
		'account'
	]);
});
