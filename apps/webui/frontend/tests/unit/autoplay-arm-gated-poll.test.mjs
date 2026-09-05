/**
 * PERFMODE-04: the AutoPlay poll exists only while AutoPlay is ARMED.
 *
 * installAutoPlay used to create its 250ms setInterval unconditionally at
 * /performance mount, for the life of the route. Disarmed, _tick returned
 * before touching a deck, so the work was already near zero - but the wakeup
 * was not, and four main-thread wakes a second is exactly the steady-state burn
 * a machine protecting an audio graph should not be paying for a feature that
 * is switched off.
 *
 * The behavior is proven by RUNNING the controller with its runes live
 * (load-rune-module.mjs compiles the real $effect.root rather than stubbing
 * it), and the shape guards below then pin the reasons - the same source-level
 * assertions autoplay-background-tab-clock.test.mjs makes about _snaps.
 *
 * [if] the interval is created outside the arm effect again [then] a disarmed
 *   AutoPlay wakes the main thread 4x/s for the whole set - broken.
 * [if] the effect stops calling _stopPoll on disarm [then] the timer leaks past
 *   the toggle and the gate does nothing - broken.
 * [if] the uninstall stops clearing the timer [then] leaving /performance
 *   leaves a poll running against a torn-down engine - broken.
 * [if] _tick reads a deck before its enabled guard [then] a disarmed poll (a
 *   toggle racing an in-flight tick) touches live scheduling state - broken.
 * [if] POLL_MS or the audio-clock read changes [then] the background-tab
 *   mixing property from 78d4c95b regresses - covered by
 *   autoplay-background-tab-clock.test.mjs, which must keep passing.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';

const SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
	'utf8'
);

/** The controller plus the real pref setter that arms it. */
const ENTRY = [
	"export { installAutoPlay } from '$lib/rb/auto-play.svelte';",
	"export { setAutoPlayEnabled, uiPrefs } from '$lib/rb/prefs.svelte';"
].join('\n');

test('RUNNING it: the poll exists exactly while AutoPlay is armed', async () => {
	const probe = installTimerProbe();
	let uninstall = null;
	try {
		const autoPlay = await loadRuneModule(ENTRY);
		assert.equal(
			autoPlay.uiPrefs.auto_play_enabled,
			true,
			'the shipped default is ARMED, so the disarmed path has to be reachable by toggle'
		);

		uninstall = autoPlay.installAutoPlay();
		await probe.flush();
		assert.equal(probe.liveIntervals(), 1, 'armed at install: the poll runs');

		autoPlay.setAutoPlayEnabled(false);
		await probe.flush();
		assert.equal(probe.liveIntervals(), 0, 'disarming stops the timer, not just the work');

		autoPlay.setAutoPlayEnabled(true);
		await probe.flush();
		assert.equal(probe.liveIntervals(), 1, 're-arming brings the same poll back');

		uninstall();
		uninstall = null;
		await probe.flush();
		assert.equal(probe.liveIntervals(), 0, 'leaving /performance leaves nothing running');
	} finally {
		// Unconditional: a failed assertion above can leave a real 250ms
		// interval running, which would hang the runner instead of failing it.
		if (uninstall !== null) uninstall();
		probe.restore();
	}
});

function blockAfter(marker) {
	const start = SOURCE.indexOf(marker);
	assert.notEqual(start, -1, `if ${marker} cannot be located then this guard asserts nothing`);
	return SOURCE.slice(start, SOURCE.indexOf('\n}', start));
}

test('the interval is created only from the arm effect', () => {
	const creations = SOURCE.split('setInterval(').length - 1;
	assert.equal(creations, 1, 'exactly one place may create the poll');
	assert.match(
		blockAfter('function _startPoll(): void {'),
		/setInterval\(/,
		'and it is _startPoll, so the effect is the only thing that decides when'
	);
});

test('arming starts the poll and disarming stops it', () => {
	const install = blockAfter('export function installAutoPlay(): () => void {');
	assert.match(install, /\$effect\.root\(\(\) => \{/);
	// Both directions, explicitly - a start with no stop leaks the timer past
	// the toggle. Asserted per branch rather than as one literal line, so the
	// arm effect stays free to do other arm/disarm work (PLAY-05 queue
	// activation) without this guard reading that as a regression.
	const armAt = install.indexOf('if (uiPrefs.auto_play_enabled)');
	assert.notEqual(armAt, -1, 'the arm effect must still branch on the pref');
	const elseAt = install.indexOf('else', armAt);
	assert.notEqual(elseAt, -1, 'and it must still have a disarm branch');
	const teardownAt = install.indexOf('\treturn () => {');
	const armed = install.slice(armAt, elseAt);
	const disarmed = install.slice(elseAt, teardownAt === -1 ? undefined : teardownAt);

	assert.match(armed, /_startPoll\(\);/, 'arming starts the poll');
	assert.doesNotMatch(armed, /_stopPoll\(\);/, 'and the arm branch never stops it');
	assert.match(disarmed, /_stopPoll\(\);/, 'disarming stops it');
	assert.doesNotMatch(disarmed, /_startPoll\(\);/, 'and the disarm branch never starts it');

	assert.doesNotMatch(
		install,
		/setInterval\(/,
		'installing must no longer create a timer by itself'
	);
});

test('installAutoPlay is still single-shot, and its uninstall clears the timer', () => {
	const install = blockAfter('export function installAutoPlay(): () => void {');
	assert.match(install, /throw new Error\('auto-play already installed'\)/);
	assert.match(install, /_stopArmWatcher\?\.\(\);/, 'the effect root must be torn down');
	assert.match(install, /_stopPoll\(\);/, 'and the interval with it');
});

test('a tick that does fire while disarmed touches no deck', () => {
	const tick = SOURCE.slice(
		SOURCE.indexOf('async function _tick(): Promise<void> {'),
		SOURCE.indexOf('function _startPoll(): void {')
	);
	assert.ok(tick.length > 0, 'if _tick cannot be located then this guard asserts nothing');

	const guard = tick.indexOf('if (!uiPrefs.auto_play_enabled) {');
	assert.notEqual(guard, -1, 'the enabled guard must still be the first thing _tick does');
	const beforeGuard = tick.slice(0, guard);
	assert.doesNotMatch(beforeGuard, /_snaps\(\)/);
	assert.doesNotMatch(beforeGuard, /deckAudioClockPositionMs\(/);
	assert.doesNotMatch(beforeGuard, /deckStates\[/);

	// A toggle can land between a scheduled tick and its callback, so the guard
	// is the belt to the effect's braces, not a redundancy to be deleted.
	const clockRead = tick.indexOf('_promoteMaster()');
	assert.ok(clockRead > guard, 'every engine read stays behind the guard');
});

/**
 * PLAY-05: the arm effect clears the charted order on every run, so it must not
 * also DEPEND on the state it clears.
 *
 * The order's publisher compares against autoPlayOrder.chain to skip an
 * unchanged plan. Read from inside this tracked $effect, that comparison turned
 * every charted plan into a reason to re-run the arm effect, which republished
 * the empty order and nulled the memo key - so the queue was wiped the instant
 * the poll produced one, and the next tick repeated the cycle. clearAutoPlayOrder
 * exists to write without reading; the source guard in
 * autoplay-order-memo.test.mjs pins that it is CALLED, and this pins that it
 * still has the property the call was made for.
 *
 * [if] the arm effect subscribes to autoPlayOrder/autoPlayQueue again [then] a
 *   planned handoff list is cleared as soon as it exists and can never be
 *   read - broken.
 * [if] the pref read is untracked by mistake [then] arming and disarming stop
 *   working entirely - broken, so the disarm leg is asserted here as well.
 */
const PLAN_ENTRY = [
	"export { installAutoPlay } from '$lib/rb/auto-play.svelte';",
	"export { autoPlayOrder, autoPlayQueue, publishAutoPlayOrder } from '$lib/rb/autoplay-queue.svelte';",
	"export { setAutoPlayTrackFeed } from '$lib/rb/auto-play';",
	"export { setAutoPlayEnabled } from '$lib/rb/prefs.svelte';"
].join('\n');

test('RUNNING it: a published plan survives, and the pref still arms/disarms', async () => {
	const probe = installTimerProbe();
	let uninstall = null;
	try {
		const autoPlay = await loadRuneModule(PLAN_ENTRY);
		uninstall = autoPlay.installAutoPlay();
		await probe.flush();
		assert.equal(autoPlay.autoPlayQueue.active, true, 'arming creates the inspectable queue');

		// Exactly what the poll does once _refreshChartedOrder has a plan.
		autoPlay.setAutoPlayTrackFeed('armed-poll', [
			{ stable_id: 'nx-1', key: '8A', bpm: 124, file_exists: true, title: 'One', artist: 'A' },
			{ stable_id: 'nx-2', key: '9A', bpm: 126, file_exists: true, title: 'Two', artist: 'B' }
		]);
		autoPlay.publishAutoPlayOrder(['nx-1', 'nx-2']);
		await probe.flush();

		assert.deepEqual(
			[...autoPlay.autoPlayOrder.chain],
			['nx-1', 'nx-2'],
			'if publishing a plan re-runs the arm effect then the order is wiped as it appears - broken'
		);
		assert.deepEqual(
			autoPlay.autoPlayQueue.entries.map((entry) => entry.stable_id),
			['nx-1', 'nx-2'],
			'and the user-viewable queue goes with it - broken'
		);

		autoPlay.setAutoPlayEnabled(false);
		await probe.flush();
		assert.equal(autoPlay.autoPlayQueue.active, false, 'disarming still clears the queue');
		assert.deepEqual([...autoPlay.autoPlayOrder.chain], [], 'and the order with it');
	} finally {
		if (uninstall !== null) uninstall();
		probe.restore();
	}
});
