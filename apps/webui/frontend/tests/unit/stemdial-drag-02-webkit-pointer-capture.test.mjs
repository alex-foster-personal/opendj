// requirement: STEMDIAL-DRAG-9210
/**
 * Source pins for the WebKit DRUMS/FILTER drag path.
 *
 * Regression lines:
 * - if Knob listens for pointermove only on itself, WebKit drops a drag after it leaves DRUMS
 * - if pointerup releases capture on currentTarget, a window-targeted release throws or no-ops
 * - if FILTER loses pointerTravelIsDrag, a tap turns the dial instead of opening its menu
 * - if wheelAdjust leaves the knob, scroll no longer turns DRUMS
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { bundleSvelteEntry } from './mount-svelte.mjs';

const knob = readFrontendSource('src/lib/components/rb/mixer/Knob.svelte');

let bundle;

before(async () => {
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	bundle = await bundleSvelteEntry(
		`
		export { default as Knob } from '$lib/components/rb/mixer/Knob.svelte';
		export { render } from 'svelte/server';
	`,
		{ 'import.meta.env.VITE_IO_DEVICE_ACCESS_ON_OPEN': 'undefined' }
	);
});

test('active knob drags move and finish through window, not element-only listeners', () => {
	const html = bundle.render(bundle.Knob, {
		props: {
			knobId: 'stemdial-drag-02',
			label: 'DRUMS',
			value: 0.5,
			size: 28,
			onchange: () => {}
		}
	}).body;
	assert.match(html, /data-knob-id="stemdial-drag-02"/);
	assert.match(knob, /window\.addEventListener\('pointermove', handlePointerMove\)/);
	assert.match(knob, /window\.addEventListener\('pointerup', handlePointerUp\)/);
	assert.match(knob, /window\.addEventListener\('pointercancel', handlePointerCancel\)/);
	assert.match(knob, /window\.removeEventListener\('pointermove', handlePointerMove\)/);
	assert.doesNotMatch(knob, /onpointermove=\{handlePointerMove\}/);
	assert.doesNotMatch(knob, /onpointerup=\{handlePointerUp\}/);
	assert.doesNotMatch(knob, /onpointercancel=/);
});

test('capture release stays on the stored knob and lost capture ends the drag', () => {
	assert.match(knob, /dragElement = e\.currentTarget as HTMLElement/);
	assert.match(knob, /captureElement\.releasePointerCapture\(pointerId\)/);
	assert.doesNotMatch(knob, /e\.currentTarget as HTMLElement\)\.releasePointerCapture/);
	assert.match(knob, /onlostpointercapture=\{handleLostPointerCapture\}/);
	assert.match(
		knob,
		/function handleLostPointerCapture\(e: PointerEvent\): void \{\s*finishPointerDrag\(e\.pointerId, false\)/
	);
});

test('FILTER tap slop and the independent scroll action stay wired', () => {
	assert.match(knob, /pointerTravelIsDrag\(e\.clientX - dragStartX, e\.clientY - dragStartY\)/);
	assert.match(knob, /onsingleclick !== undefined \|\| ontap !== undefined \|\| altPressInGroup/);
	assert.match(knob, /use:wheelAdjust=\{\{/);
});

test('component teardown removes an unfinished window drag', () => {
	assert.match(knob, /onDestroy\(\(\) => \{\s*cancelPointerDrag\(\)/);
	assert.match(knob, /removeWindowDragListeners\(\)/);
});
