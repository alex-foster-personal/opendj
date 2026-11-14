// requirement: EDGESAMP-11954, EDGESAMP-14180, EDGESAMP-14535
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let wave;
let ssr;
before(async () => {
	wave = await loadTypeScriptModule('src/lib/player/sampler/edge-wave.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as EdgeWave } from '$lib/components/rb/sampler/EdgeWave.svelte';",
			"export { DEFAULT_WAVE } from '$lib/player/sampler/edge-wave';",
			"export * as ui from '$lib/player/sampler/sampler-store.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{ css: 'injected' }
	);
});

/** Markup and scoped CSS of one server-rendered EdgeWave. */
const renderWave = (edge = 'R') => {
	const { head, body } = ssr.render(ssr.EdgeWave, {
		props: {
			edge,
			top: 28,
			height: 800,
			params: ssr.DEFAULT_WAVE,
			enabled: true,
			reduceMotion: true
		}
	});
	return {
		body,
		css: [...head.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n')
	};
};

const wake = (o) => ({
	enabled: true,
	fromEdgePx: 8,
	buttonsHeld: false,
	appDrag: false,
	armed: true,
	dwellMs: 80,
	...o
});

const tabIn = (o) => ({
	awake: true,
	dy: 0,
	tabH: 32,
	held: false,
	filled: true,
	ramp: 1,
	...o
});

const baseMotionIn = (o) => ({
	enabled: true,
	awake: false,
	armed: true,
	fromEdgePx: 8,
	pointerY: 200,
	top: 28,
	headH: 132,
	tabH: 32,
	tabCount: 20,
	buttons: 0,
	inWindow: true,
	t: 1000,
	appDrag: false,
	held: Array(20).fill(false),
	filled: Array(20).fill(true),
	params: wave.DEFAULT_WAVE,
	reduceMotion: false,
	dtMs: 16,
	headW: wave.REST_W,
	widths: Array(20).fill(wave.REST_W),
	...o
});

test('[if] pointer is in the zone 60ms with no button held [then] the edge wakes', () => {
	assert.equal(wave.shouldWake(wake({ dwellMs: 80 }), wave.DEFAULT_WAVE), true);
	assert.equal(wave.shouldWake(wake({ buttonsHeld: true }), wave.DEFAULT_WAVE), false);
});

test('[if] a drag ends inside the zone [then] the edge stays disarmed until leave', () => {
	const armed = { value: true };
	wave.updateArming(armed, 40, 8, 1, false);
	assert.equal(armed.value, false);
	assert.equal(
		wave.shouldWake(wake({ armed: armed.value, buttonsHeld: false }), wave.DEFAULT_WAVE),
		false
	);
	wave.updateArming(armed, 8, 20, 0, false);
	assert.equal(armed.value, true);
});

test('[if] a deck loop drag from inside the app reaches the zone [then] the edge wakes', () => {
	/** [if] an app drag enters the zone with a button held [then] it stays armed and wakes; edge drags still do not, [else stop]. */
	const armed = { value: true };
	wave.updateArming(armed, 40, 8, 1, true);
	assert.equal(armed.value, true);
	assert.equal(
		wave.shouldWake(wake({ buttonsHeld: true, appDrag: true }), wave.DEFAULT_WAVE),
		true
	);
	assert.equal(
		wave.shouldWake(wake({ buttonsHeld: true, armed: false, appDrag: true }), wave.DEFAULT_WAVE),
		true
	);
	wave.updateArming(armed, 40, 8, 1, false);
	assert.equal(
		wave.shouldWake(wake({ buttonsHeld: true, armed: armed.value }), wave.DEFAULT_WAVE),
		false
	);
});

test('[if] the edge is awake [then] tab width follows the Gaussian with spread 2.2', () => {
	const w0 = wave.tabTargetWidth(tabIn({ dy: 0 }), wave.DEFAULT_WAVE);
	assert.equal(w0, wave.DEFAULT_WAVE.pop);
	const wFar = wave.tabTargetWidth(tabIn({ dy: 500 }), wave.DEFAULT_WAVE);
	assert.ok(wFar - wave.AWAKE_W < 1, String(wFar));
	const eased = wave.easeWidth(3, 32, 16, wave.DEFAULT_WAVE, false);
	assert.ok(eased > 3 && eased < 32);
	assert.equal(wave.easeWidth(3, 32, 90, wave.DEFAULT_WAVE, true), 32);
});

test('[if] the pointer leaves the stay band [then] the edge collapses after 250ms', () => {
	const m = wave.createEdgeMotion(20);
	wave.stepEdgeMotion(m, baseMotionIn({ t: 10 }));
	wave.stepEdgeMotion(m, baseMotionIn({ t: 80 }));
	assert.equal(m.awake, true);
	wave.stepEdgeMotion(m, baseMotionIn({ t: 90, fromEdgePx: 400 }));
	assert.equal(m.leaveT, 90);
	wave.stepEdgeMotion(m, baseMotionIn({ t: 341, fromEdgePx: 400 }));
	assert.equal(m.awake, false);
});

test('[if] one edge is awake [then] the other edge state is unchanged', () => {
	const left = wave.createEdgeMotion(20);
	const right = wave.createEdgeMotion(20);
	wave.stepEdgeMotion(left, baseMotionIn({ t: 10 }));
	wave.stepEdgeMotion(left, baseMotionIn({ t: 80 }));
	assert.equal(left.awake, true);
	assert.equal(right.awake, false);
});

test('[if] no edge is awake and nothing is easing [then] motion is idle', () => {
	const m = wave.createEdgeMotion(20);
	assert.equal(wave.motionIdle(m), true);
});

test('[if] the sampler is off [then] the edge never wakes', () => {
	assert.equal(wave.shouldWake(wake({ enabled: false }), wave.DEFAULT_WAVE), false);
	const m = wave.createEdgeMotion(20);
	wave.stepEdgeMotion(m, baseMotionIn({ enabled: false, t: 200 }));
	assert.equal(m.awake, false);
});

test('a set holds 20 tabs per edge', () => {
	/** [if] a set is shown [then] DEFAULT_WAVE.perSet is 20 and each rendered edge has 20 slots, [else stop]. */
	assert.equal(wave.DEFAULT_WAVE.perSet, 20);
	for (const edge of ['L', 'R']) {
		assert.equal(renderWave(edge).body.match(/class="wslot[^"]*"/g)?.length, 20, edge);
		assert.match(renderWave(edge).body, /data-tab="20"/);
	}
});

test('a tab under the pointer pops to 476 px and the top tab opens to 476 px', () => {
	/** [if] the pointer is on a tab centre while awake [then] its target is 476 px and the head eases to 476 px, [else stop]. */
	assert.equal(wave.tabTargetWidth(tabIn({ dy: 0 }), wave.DEFAULT_WAVE), 476);
	assert.equal(wave.HEAD_OPEN_W, 476);
	assert.equal(wave.HEAD_H, 132);
	const m = wave.createEdgeMotion(20);
	const at = (t) => baseMotionIn({ t, reduceMotion: true, headW: m.headW, widths: m.widths });
	wave.stepEdgeMotion(m, at(10));
	wave.stepEdgeMotion(m, at(80));
	assert.equal(m.awake, true);
	assert.equal(m.headW, 476);
	assert.match(renderWave().body, /class="whead[^"]*"[^>]*style="[^"]*height: 130px/);
});

test('with no catalogue every slot is empty, inert and drawn with the dashed empty look', () => {
	/** [if] no catalogue is wired [then] all 20 slots are dashed empties that ignore clicks and digit keys, [else stop]. */
	const { body, css } = renderWave();
	assert.equal(body.match(/class="wslot empty rest\b/g)?.length, 20);
	assert.equal(body.match(/empty: drag a loop hot cue here/g)?.length, 20);
	assert.match(
		css,
		/\.wslot\.empty[^{]*\{[^}]*border-style: ?dashed[^}]*var\(--es-border-strong\)/
	);
	assert.equal(ssr.ui.edgeItems('R').length, 0);
	assert.equal(ssr.ui.pinEdgeTabOnClick('R', 0, 476, 1000), false);
	assert.equal(ssr.ui.sampler.pinned.length, 0);
	const empty = wave.tabTargetWidth(tabIn({ filled: false, held: true }), wave.DEFAULT_WAVE);
	assert.equal(empty, wave.EMPTY_POP_W);
	assert.equal(wave.tabTargetWidth(tabIn({ filled: false, awake: false }), wave.DEFAULT_WAVE), 0);
});

test('a click on a narrow or just-woken tab does nothing', async () => {
	/** [if] a tab is under 150 px wide or woke under 150 ms ago [then] the click is refused and nothing pins, [else stop]. */
	assert.equal(wave.clickAllowed(149, 1000), false);
	assert.equal(wave.clickAllowed(476, 149), false);
	assert.equal(wave.clickAllowed(150, 150), true);
	// The L edge shows set 2; its third slot is filled, and PUT /pins answers like the route.
	const st = ssr.ui.sampler;
	const filled = { slot_id: 's2', color: '#3fa7ff' };
	st.sets = [
		{ id: 'set-1', name: 'Set 1', description: '', items: [] },
		{ id: 'set-2', name: 'Set 2', description: '', items: [null, null, filled] }
	];
	ssr.ui.setSamplerFetch(async () => new Response(JSON.stringify({ pins: ['s2'] })));
	try {
		assert.equal(ssr.ui.pinEdgeTabOnClick('L', 2, 120, 1000), false);
		assert.equal(ssr.ui.pinEdgeTabOnClick('L', 2, 476, 100), false);
		assert.equal(ssr.ui.isPinned('s2'), false);
		assert.equal(ssr.ui.pinEdgeTabOnClick('L', 2, 476, 1000), true);
		assert.equal(ssr.ui.isPinned('s2'), true);
		await new Promise((r) => setTimeout(r, 20));
		assert.deepEqual(st.pinned, ['s2']);
	} finally {
		st.sets = [];
		st.pinned = [];
	}
});

test('a set flip pops tabs top to bottom and controls fade in by width', () => {
	/** [if] a set flips [then] tab i ramps from 18*i ms over 140 ms, and reveals follow the mock ramps, [else stop]. */
	assert.equal(wave.flipRamp(1000, 1000, 0), 0);
	assert.equal(wave.flipRamp(1070, 1000, 0), 0.5);
	assert.equal(wave.flipRamp(1070, 1000, 3), 16 / 140);
	assert.equal(wave.flipRamp(1400, 1000, 19), 58 / 140);
	assert.equal(wave.tabTargetWidth(tabIn({ ramp: 0 }), wave.DEFAULT_WAVE), wave.AWAKE_W);
	assert.equal(wave.controlsReveal(70), 0);
	assert.equal(wave.controlsReveal(160), 1);
	assert.equal(wave.titleReveal(425), 0.5);
	assert.equal(wave.tabRef(0, 6), 'S1-07');
});
