// requirement: MIXUX-08
// [if] ghost diverges [then] idle threshold and reversal helpers fire guidance
// [if] ghost and software values reconcile [then] ghost clears
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ghost;

before(async () => {
	ghost = await loadTypeScriptModule('src/lib/rb/fader-ghost.svelte.ts');
});

test('values reconcile within epsilon', () => {
	assert.equal(ghost.valuesReconciled(0.5, 0.505), true);
	assert.equal(ghost.valuesReconciled(0.5, 0.52), false);
});

test('idle threshold is 2000ms', () => {
	const now = 10_000;
	assert.equal(ghost.shouldShowGuidanceAfterIdle(now - 1999, now), false);
	assert.equal(ghost.shouldShowGuidanceAfterIdle(now - 2000, now), true);
});

test('guidance cycle constant is 3000ms', () => {
	assert.equal(ghost.FADER_GUIDANCE_CYCLE_MS, 3000);
});

test('pointer reversal detection', () => {
	assert.equal(ghost.detectPointerReversal(1, -1), true);
	assert.equal(ghost.detectPointerReversal(1, 1), false);
});

test('openGhost clears when reconciled immediately', () => {
	ghost.openGhost(1, 0.4, 0.4);
	assert.equal(ghost.faderGhostByDeck[1].diverged, false);
});

test('tickFaderGhostIdle shows guidance after 2s idle without pointermove', () => {
	ghost.clearGhost(2);
	ghost.openGhost(2, 0.2, 0.8);
	const idleSince = ghost.faderGhostByDeck[2].idleSince;
	assert.notEqual(idleSince, null);
	assert.equal(ghost.guidanceVisible(2), false);
	ghost.tickFaderGhostIdle(idleSince + 1999);
	assert.equal(ghost.guidanceVisible(2), false);
	ghost.tickFaderGhostIdle(idleSince + 2001);
	assert.equal(ghost.guidanceVisible(2), true);
});
