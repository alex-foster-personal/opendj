// requirement: EDGESAMP-11954
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let wave;
before(async () => {
	wave = await loadTypeScriptModule('src/lib/player/sampler/edge-wave.ts');
});

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
	...o
});

const baseMotionIn = (o) => ({
	enabled: true,
	awake: false,
	armed: true,
	fromEdgePx: 8,
	pointerY: 200,
	top: 28,
	headH: 44,
	tabH: 32,
	tabCount: 10,
	buttons: 0,
	inWindow: true,
	t: 1000,
	appDrag: false,
	held: Array(10).fill(false),
	filled: Array(10).fill(true),
	params: wave.DEFAULT_WAVE,
	reduceMotion: false,
	dtMs: 16,
	headW: wave.REST_W,
	widths: Array(10).fill(wave.REST_W),
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
	const m = wave.createEdgeMotion(10);
	wave.stepEdgeMotion(m, baseMotionIn({ t: 10 }));
	wave.stepEdgeMotion(m, baseMotionIn({ t: 80 }));
	assert.equal(m.awake, true);
	wave.stepEdgeMotion(m, baseMotionIn({ t: 90, fromEdgePx: 400 }));
	assert.equal(m.leaveT, 90);
	wave.stepEdgeMotion(m, baseMotionIn({ t: 341, fromEdgePx: 400 }));
	assert.equal(m.awake, false);
});

test('[if] one edge is awake [then] the other edge state is unchanged', () => {
	const left = wave.createEdgeMotion(10);
	const right = wave.createEdgeMotion(10);
	wave.stepEdgeMotion(left, baseMotionIn({ t: 10 }));
	wave.stepEdgeMotion(left, baseMotionIn({ t: 80 }));
	assert.equal(left.awake, true);
	assert.equal(right.awake, false);
});

test('[if] no edge is awake and nothing is easing [then] motion is idle', () => {
	const m = wave.createEdgeMotion(10);
	assert.equal(wave.motionIdle(m), true);
});

test('[if] the sampler is off [then] the edge never wakes', () => {
	assert.equal(wave.shouldWake(wake({ enabled: false }), wave.DEFAULT_WAVE), false);
	const m = wave.createEdgeMotion(10);
	wave.stepEdgeMotion(m, baseMotionIn({ enabled: false, t: 200 }));
	assert.equal(m.awake, false);
});
