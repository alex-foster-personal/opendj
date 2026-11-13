/**
 * D6 (#9373, issue #9717): Knob and VFader render a commanded move's target ghost and progress
 * from the runner's presentation seam, rendered for real through svelte's SSR renderer.
 *
 * Regression lines:
 * - if a running commanded move does not draw a ghost at its target on Knob or VFader then broken
 * - if the ghost moves aria-valuenow or the indicator/thumb off the control's own value then broken
 * - if the ghost or progress mark is focusable, has a role, or sits outside the aria-hidden art then broken
 * - if the ghost-only variant still draws the progress mark then broken
 * - if a cancelled move leaves a ghost behind then broken
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const SSR_ENTRY = [
	"export { default as Knob } from '$lib/components/rb/mixer/Knob.svelte';",
	"export { default as VFader } from '$lib/components/rb/mixer/VFader.svelte';",
	"export { _resetCommandedMovesForTests, _setCommandedMoveClockForTests, startCommandedMove } from '$lib/rb/commanded-move-runner.svelte';",
	"export { _resetKnobRegistryForTests, nudgeKnobOrSelection, registerKnob } from '$lib/rb/knob-control.svelte';",
	"export { readVariant, setVariant } from '$lib/rb/prefs.svelte';",
	"export { COMMANDED_GHOST_VARIANT } from '$lib/rb/commanded-move-presentation';",
	"export { render } from 'svelte/server';"
].join('\n');

const SWEEP_DEG = 270;

let ssr;
let t;
/** @type {Array<() => void>} */
let pending;

before(async () => {
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	ssr = await loadSvelteSsrModule(SSR_ENTRY);
});

after(() => {
	ssr._resetCommandedMovesForTests();
});

beforeEach(() => {
	ssr._resetKnobRegistryForTests();
	ssr._resetCommandedMovesForTests();
	ssr.setVariant(ssr.COMMANDED_GHOST_VARIANT.id, 'ghost-arc');
	t = 1000;
	pending = [];
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

function startMove(id, from, to) {
	let v = from;
	ssr.registerKnob({ id, getValue: () => v, setValue: (x) => (v = x) });
	return ssr.startCommandedMove(id, (start, now) => ({
		startMs: now,
		endMs: now + 1000,
		valueAt: (tm) => start + (to - start) * Math.min(1, Math.max(0, (tm - now) / 1000))
	}));
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

function elementWithTestId(html, testId) {
	const match = html.match(new RegExp(`<[a-z]+[^>]*data-testid="${testId}"[^>]*>`));
	assert.ok(match, `no element with data-testid=${testId}`);
	return match[0];
}

test('knob draws the target ghost and progress ring from the seam, leaving the value authoritative', async () => {
	/** [if] a move to 1.0 runs half way on a dial at 0.5 [then] ghost sits at +135deg, ring shows, aria-valuenow stays 0.5, [else stop]. */
	startMove('1:low', 0, 1);
	await step(500);
	const html = renderKnob(0.5);
	const ghost = elementWithTestId(html, 'commanded-ghost-1:low');
	assert.match(ghost, new RegExp(`rotate\\(${(1 - 0.5) * SWEEP_DEG}deg\\)`));
	assert.match(elementWithTestId(html, 'commanded-progress-1:low'), /stroke-dasharray="43\.98 /);
	assert.match(html, /aria-valuenow="0\.5"/);
	assert.match(html, /data-commanded-target="1"/);
	assert.match(html, /data-commanded-progress="0\.5"/);
	assert.match(html, /<g style="transform: rotate\(0deg\);[^"]*"[^>]*><line[^>]*class="indicator/);
});

test('knob ghost is art only: inside the aria-hidden svg, no role, no tabindex', async () => {
	/** [if] a ghost renders [then] it lives inside the aria-hidden svg and carries no role or tabindex, [else stop]. */
	startMove('1:low', 0, 1);
	const html = renderKnob(0.5);
	const svg = html.slice(html.indexOf('<svg'), html.indexOf('</svg>'));
	assert.match(svg, /^<svg[^>]*aria-hidden="true"/);
	const ghost = elementWithTestId(svg, 'commanded-ghost-1:low');
	assert.doesNotMatch(ghost, /role=|tabindex=/);
});

test('fader draws the target mark at the target and keeps its thumb on the value', async () => {
	/** [if] a move to 1.0 runs on fader 1 at 0.25 [then] the mark sits at top 0 with a progress fill, thumb stays at 0.25, [else stop]. */
	startMove('1:fader', 0, 1);
	await step(250);
	const html = renderFader(0.25);
	const mark = elementWithTestId(html, 'commanded-ghost-1:fader');
	assert.match(mark, /style="top: 0px; height: 12px;"/);
	assert.doesNotMatch(mark, /role=|tabindex=/);
	assert.match(elementWithTestId(html, 'commanded-progress-1:fader'), /width: 25\.0%;/);
	assert.match(html, /aria-valuenow="0\.25"/);
	assert.match(html, /class="rb-fader-thumb[^"]*" style="top: 63px;"/);
});

test('ghost-only variant draws the ghost and no progress mark', () => {
	/** [if] the variant is ghost-only [then] Knob and VFader render the ghost but no progress element, [else stop]. */
	ssr.setVariant(ssr.COMMANDED_GHOST_VARIANT.id, 'ghost-only');
	startMove('1:low', 0, 1);
	startMove('1:fader', 0, 1);
	const knob = renderKnob(0.5);
	const fader = renderFader(0.5);
	elementWithTestId(knob, 'commanded-ghost-1:low');
	elementWithTestId(fader, 'commanded-ghost-1:fader');
	assert.doesNotMatch(knob + fader, /commanded-progress-/);
});

test('a cancelled move leaves no ghost on either control', async () => {
	/** [if] the hand cancels both moves [then] neither control renders a ghost or commanded data attribute, [else stop]. */
	startMove('1:low', 0, 1);
	startMove('1:fader', 0, 1);
	ssr.nudgeKnobOrSelection('1:low', 0.01);
	ssr.nudgeKnobOrSelection('1:fader', 0.01);
	const html = renderKnob(0.5) + renderFader(0.5);
	assert.doesNotMatch(html, /commanded-ghost-|commanded-progress-|data-commanded-/);
});
