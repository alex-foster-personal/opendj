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

// [if] a diverged ghost stays idle past the threshold [then] guidance is written ONCE, not every frame
test('tickFaderGhostIdle writes guidance once, not on every animation frame', () => {
	ghost.clearGhost(3);
	ghost.openGhost(3, 0.1, 0.9);
	const idleSince = ghost.faderGhostByDeck[3].idleSince;
	ghost.tickFaderGhostIdle(idleSince + 2001);
	const written = ghost.faderGhostByDeck[3];
	assert.equal(written.showGuidance, true);
	for (let frame = 1; frame <= 10; frame++) ghost.tickFaderGhostIdle(idleSince + 2001 + frame * 16);
	assert.equal(ghost.faderGhostByDeck[3], written, 'each frame replaced the deck state: every ghost reader re-ran at 60 Hz');
});

/** Real timers only. A pending `setTimeout` is observed through Node's own
 * resource accounting, not by replacing the timer API. */
const armedTimeouts = () => process.getActiveResourcesInfo().filter((kind) => kind === 'Timeout').length;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
/** Poll real time until `predicate` holds or `ms` passes. */
async function within(ms, predicate) {
	const deadline = performance.now() + ms;
	while (performance.now() < deadline) {
		if (predicate()) return true;
		await sleep(10);
	}
	return predicate();
}

test('control: the armed-timer probe counts a real pending setTimeout', () => {
	const before = armedTimeouts();
	const id = setTimeout(() => {}, 60_000);
	assert.equal(armedTimeouts(), before + 1, 'the probe must see a real armed timer, or every absence below is blind');
	clearTimeout(id);
	assert.equal(armedTimeouts(), before);
});

// [if] no deck is diverged [then] the idle ticker arms no timer (no idle frame clock)
// [if] a deck diverges [then] one timer is armed, and guidance shows once FADER_IDLE_MS of real time passes
// [if] guidance is shown or the ghost clears [then] no timer stays armed
test('idle ticker runs only while a ghost is diverged and waiting', async () => {
	for (const deck of [1, 2, 3, 4]) ghost.clearGhost(deck);
	const baseline = armedTimeouts();
	const uninstall = ghost.installFaderGhostIdleTicker();
	try {
		assert.equal(armedTimeouts(), baseline, 'if no divergence arms a timer then the idle clock is back');
		await sleep(50);
		assert.equal(armedTimeouts(), baseline, 'nothing arms later on its own either');
		const opened = performance.now();
		ghost.openGhost(4, 0.1, 0.9);
		assert.equal(armedTimeouts(), baseline + 1, 'a diverged ghost arms exactly one timer');
		assert.equal(ghost.guidanceVisible(4), false);
		const shown = await within(ghost.FADER_IDLE_MS + 1000, () => ghost.guidanceVisible(4));
		const waited = performance.now() - opened;
		assert.equal(shown, true, 'if the timer does not show guidance then the hint never appears');
		assert.ok(waited >= ghost.FADER_IDLE_MS - 5, `guidance showed after ${waited} ms, before the idle threshold`);
		assert.equal(armedTimeouts(), baseline, 'if a timer stays armed after guidance shows then it ticks forever');
		ghost.clearGhost(4);
		assert.equal(armedTimeouts(), baseline, 'a cleared ghost must not re-arm the timer');
	} finally {
		uninstall();
		for (const deck of [1, 2, 3, 4]) ghost.clearGhost(deck);
	}
});

// [if] a pointer move resets idleSince [then] guidance waits a full FADER_IDLE_MS from the move
// [if] a ghost clears while its timer is pending [then] the timer is cancelled
// [if] the ticker is uninstalled [then] a pending timer is cancelled and nothing re-arms
test('idle ticker follows the deadline, cancels on clear and on uninstall', async () => {
	for (const deck of [1, 2, 3, 4]) ghost.clearGhost(deck);
	const baseline = armedTimeouts();
	let uninstall = ghost.installFaderGhostIdleTicker();
	try {
		ghost.openGhost(1, 0.2, 0.8);
		await sleep(ghost.FADER_IDLE_MS / 2);
		// A pointer move resets idleSince: the deadline moves with it.
		ghost.onFaderPointerMove(1, 100, 0.8, 50, 200);
		const moved = performance.now();
		await sleep(ghost.FADER_IDLE_MS / 2 + 200);
		assert.equal(ghost.guidanceVisible(1), false, 'if the deadline ignores the pointer move then guidance shows early');
		assert.equal(armedTimeouts(), baseline + 1, 'still waiting on the moved deadline');
		const shown = await within(ghost.FADER_IDLE_MS, () => ghost.guidanceVisible(1));
		assert.equal(shown, true, 'guidance shows once the moved deadline passes');
		assert.ok(performance.now() - moved >= ghost.FADER_IDLE_MS - 5);
		assert.equal(armedTimeouts(), baseline);

		ghost.openGhost(2, 0.1, 0.9);
		assert.equal(armedTimeouts(), baseline + 1);
		ghost.clearGhostIfReconciled(2, 0.1);
		assert.equal(armedTimeouts(), baseline, 'if a reconciled ghost leaves its timer armed then it fires for nothing');

		ghost.openGhost(3, 0.1, 0.9);
		assert.equal(armedTimeouts(), baseline + 1);
		uninstall();
		uninstall = () => {};
		assert.equal(armedTimeouts(), baseline, 'if uninstall leaves the timer armed then it outlives the route');
		ghost.openGhost(3, 0.1, 0.9);
		assert.equal(armedTimeouts(), baseline, 'an uninstalled ticker must not arm');
	} finally {
		uninstall();
		for (const deck of [1, 2, 3, 4]) ghost.clearGhost(deck);
	}
});
