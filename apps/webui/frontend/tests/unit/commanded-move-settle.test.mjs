/**
 * #14036 (epic #9373): a completed commanded move lands with a short visual settle on Knob and
 * VFader. The curve is a pure function; the controls are rendered for real through svelte SSR.
 *
 * Regression lines:
 * - if a completed move's settle does not start above 0 and reach 0 within one beat then broken
 * - if reduced motion leaves any settle offset or settle markup then broken
 * - if the `none` setting still runs a settle then broken
 * - if the settle moves the control value, aria-valuenow or the indicator/thumb base position then broken
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const SSR_ENTRY = [
	"export { default as Knob } from '$lib/components/rb/mixer/Knob.svelte';",
	"export { default as VFader } from '$lib/components/rb/mixer/VFader.svelte';",
	"export { _resetCommandedMovesForTests, _setCommandedMoveClockForTests, startCommandedMove } from '$lib/rb/commanded-move-runner.svelte';",
	"export { _resetKnobRegistryForTests, readKnobValue, registerKnob } from '$lib/rb/knob-control.svelte';",
	"export * as settle from '$lib/rb/commanded-move-presentation';",
	"export { setReduceMotionPref } from '$lib/motion/reduced-motion';",
	"export { setVariant } from '$lib/rb/prefs.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

/** One beat at 180 BPM, faster than any commanded-move tempo a set plays at. */
const FAST_BEAT_MS = 60_000 / 180;
const ON = { reducedMotion: false, style: 'settle' };

let ssr;
let t;
/** @type {Array<() => void>} */
let pending;
/** @type {Record<string, number>} */
let values;

before(async () => {
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	ssr = await loadSvelteSsrModule(SSR_ENTRY);
});

after(() => {
	ssr._resetCommandedMovesForTests();
	ssr.setReduceMotionPref(false);
});

beforeEach(() => {
	ssr._resetKnobRegistryForTests();
	ssr._resetCommandedMovesForTests();
	ssr.setReduceMotionPref(false);
	ssr.setVariant(ssr.settle.COMMANDED_SETTLE_VARIANT.id, 'settle');
	t = 1000;
	pending = [];
	values = {};
	ssr._setCommandedMoveClockForTests({
		now: () => t,
		nextFrame: () => new Promise((resolve) => pending.push(resolve))
	});
});

async function step(ms) {
	t += ms;
	const rs = pending;
	pending = [];
	for (const r of rs) r();
	await new Promise((resolve) => setImmediate(resolve));
}

/** Run a 1 s commanded move on `id` from `from` to `to` until the runner reports it done. */
async function completeMove(id, from, to) {
	values[id] = from;
	ssr.registerKnob({ id, getValue: () => values[id], setValue: (x) => (values[id] = x) });
	const handle = ssr.startCommandedMove(id, (start, now) => ({
		startMs: now,
		endMs: now + 1000,
		valueAt: (tm) => start + (to - start) * Math.min(1, Math.max(0, (tm - now) / 1000))
	}));
	await step(500);
	await step(600);
	assert.equal((await handle.done).outcome, 'completed');
}

function renderKnob(value) {
	return ssr.render(ssr.Knob, {
		props: { knobId: '1:low', label: 'LOW', value, onchange: () => {} }
	}).body;
}

function renderFader(value) {
	return ssr.render(ssr.VFader, {
		props: { value, onchange: () => {}, label: 'channel 1 fader', deckId: 1 }
	}).body;
}

function settleVar(html, testId, i) {
	const el = html.match(new RegExp(`<[a-z]+[^>]*data-testid="${testId}"[^>]*>`));
	assert.ok(el, `no element with data-testid=${testId}`);
	const m = el[0].match(new RegExp(`--settle-${i}: (-?[0-9.]+)(deg|px);`));
	assert.ok(m, `no --settle-${i} on ${testId}`);
	return Number(m[1]);
}

test('a completed move settles from above 0 back to 0 within one beat', async () => {
	/** [if] a move completes [then] the settle offset starts above 0 and is 0 within one beat, [else stop]. */
	const { commandedSettleOffset, COMMANDED_SETTLE_MS, KNOB_SETTLE_DEG } = ssr.settle;
	assert.ok(commandedSettleOffset(0, KNOB_SETTLE_DEG, 1, ON) > 0);
	assert.ok(COMMANDED_SETTLE_MS <= FAST_BEAT_MS);
	assert.equal(commandedSettleOffset(COMMANDED_SETTLE_MS, KNOB_SETTLE_DEG, 1, ON), 0);
	assert.equal(commandedSettleOffset(FAST_BEAT_MS, KNOB_SETTLE_DEG, 1, ON), 0);
	await completeMove('1:low', 0.2, 0.8);
	await completeMove('1:fader', 0.2, 0.8);
	const knob = renderKnob(values['1:low']);
	const fader = renderFader(values['1:fader']);
	assert.ok(settleVar(knob, 'commanded-settle-1:low', 0) > 0, 'knob overshoots past the target');
	assert.equal(settleVar(knob, 'commanded-settle-1:low', 4), 0);
	assert.ok(settleVar(fader, 'commanded-settle-1:fader', 0) < 0, 'rising fader overshoots upward');
	assert.equal(settleVar(fader, 'commanded-settle-1:fader', 4), 0);
});

test('reduced motion keeps every settle offset at 0 and renders no settle', async () => {
	/** [if] reduced motion is on [then] the settle offset is 0 at every instant and no control renders a settle, [else stop]. */
	const { commandedSettleOffset, COMMANDED_SETTLE_MS } = ssr.settle;
	const reduced = { reducedMotion: true, style: 'settle' };
	for (let ms = 0; ms <= COMMANDED_SETTLE_MS; ms += 10) {
		assert.equal(commandedSettleOffset(ms, 4, 1, reduced), 0);
		assert.equal(commandedSettleOffset(ms, 4, -1, reduced), 0);
	}
	ssr.setReduceMotionPref(true);
	await completeMove('1:low', 0.2, 0.8);
	await completeMove('1:fader', 0.8, 0.2);
	const html = renderKnob(values['1:low']) + renderFader(values['1:fader']);
	assert.doesNotMatch(html, /commanded-settle-|data-commanded-settle|--settle-/);
});

test('the none setting runs no settle', async () => {
	/** [if] the settle setting is none [then] the curve is 0 and neither control renders a settle, [else stop]. */
	const { commandedSettleOffset, commandedSettleVars } = ssr.settle;
	const none = { reducedMotion: false, style: 'none' };
	assert.equal(commandedSettleOffset(0, 4, 1, none), 0);
	assert.equal(commandedSettleVars(4, 1, 'deg', none), null);
	ssr.setVariant(ssr.settle.COMMANDED_SETTLE_VARIANT.id, 'none');
	await completeMove('1:low', 0.2, 0.8);
	await completeMove('1:fader', 0.2, 0.8);
	const html = renderKnob(values['1:low']) + renderFader(values['1:fader']);
	assert.doesNotMatch(html, /commanded-settle-|data-commanded-settle|--settle-/);
});

test('a running settle never changes the control value or aria-valuenow', async () => {
	/** [if] the settle runs [then] the control value, aria-valuenow and indicator/thumb base stay on the target, [else stop]. */
	await completeMove('1:low', 0.5, 1);
	await completeMove('1:fader', 0.5, 0.25);
	assert.equal(values['1:low'], 1);
	assert.equal(values['1:fader'], 0.25);
	const knob = renderKnob(values['1:low']);
	const fader = renderFader(values['1:fader']);
	settleVar(knob, 'commanded-settle-1:low', 0);
	settleVar(fader, 'commanded-settle-1:fader', 0);
	assert.match(knob, /aria-valuenow="1"/);
	assert.match(
		knob,
		/<g style="transform: rotate\(135deg\);[^"]*"[^>]*><line[^>]*class="indicator/
	);
	assert.match(fader, /aria-valuenow="0\.25"/);
	assert.match(fader, /class="rb-fader-thumb[^"]*"[^>]*style="top: 63px; --settle-ms/);
	assert.equal(ssr.readKnobValue('1:low'), 1);
	assert.equal(ssr.readKnobValue('1:fader'), 0.25);
});
