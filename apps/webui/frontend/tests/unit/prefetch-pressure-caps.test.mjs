/**
 * PERFMODE-04 Q29 driver for the audio prefetch cache caps.
 *
 * Dependency-injected (see prefetch-pressure-caps.ts's header): the real
 * anyDeckPlaying/subscribeMachinePressure signals pull in
 * audio-engine.svelte.ts, so this test drives the module with fakes instead,
 * exactly like createBackgroundDemandShed's own tests drive it with a fake
 * isPlaying/pressureElevated/readXruns.
 */
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/prefetch-pressure-caps.ts');
});

beforeEach(() => {
	mod.resetPressureScaledPrefetchCaps();
});

function armWithFakes({ playing = true } = {}) {
	const state = { playing, elevated: false, xruns: 0 };
	const listeners = [];
	let applyCapsCalls = 0;
	const stop = mod.armPrefetchPressureCapScaling({
		isPlaying: () => state.playing,
		pressureElevated: () => state.elevated,
		readXruns: () => state.xruns,
		applyCaps: () => {
			applyCapsCalls += 1;
		},
		subscribe: (onTick) => {
			listeners.push(onTick);
			return () => {
				const i = listeners.indexOf(onTick);
				if (i !== -1) listeners.splice(i, 1);
			};
		}
	});
	return {
		state,
		stop,
		tick: () => listeners.forEach((l) => l()),
		applyCallCount: () => applyCapsCalls
	};
}

const STANDARD_TRACKS = 4;
const STANDARD_BYTES = 48 * 1024 * 1024;

test('while idle, elevated pressure never shrinks the cap', () => {
	const { state, tick, stop } = armWithFakes({ playing: false });
	state.elevated = true;
	tick();
	assert.equal(mod.pressureScaledPrefetchTrackCap(STANDARD_TRACKS), STANDARD_TRACKS);
	assert.equal(mod.pressureScaledPrefetchByteCap(STANDARD_BYTES), STANDARD_BYTES);
	stop();
});

test('while playing, elevated pressure shrinks the byte cap; clearing grows it back', () => {
	const { state, tick, stop } = armWithFakes({ playing: true });

	state.elevated = true;
	tick();
	const shrunk = mod.pressureScaledPrefetchByteCap(STANDARD_BYTES);
	assert.ok(shrunk < STANDARD_BYTES, 'the work effect (a smaller cap) must be present while elevated and playing');

	state.elevated = false;
	tick();
	tick();
	tick();
	assert.equal(
		mod.pressureScaledPrefetchByteCap(STANDARD_BYTES),
		STANDARD_BYTES,
		'the cap must return to baseline once pressure clears'
	);
	stop();
});

test('a live xrun-window delta is treated as elevated even if pressureElevated() reports false', () => {
	const { state, tick, stop } = armWithFakes({ playing: true });
	state.elevated = false;
	state.xruns = 1; // S1_XRUN_DELTA_MAX is 0, so any xrun this tick counts.
	tick();
	assert.ok(
		mod.pressureScaledPrefetchByteCap(STANDARD_BYTES) < STANDARD_BYTES,
		'an xrun must shrink the cap even without a pressure-elevated reading'
	);
	stop();
});

test('applyCaps is invoked on every tick so eviction re-runs against the new caps', () => {
	const { applyCallCount, tick, stop } = armWithFakes({ playing: true });
	tick();
	tick();
	assert.equal(applyCallCount(), 2);
	stop();
});

test('stop() tears down the subscription: further ticks (via a manual call) have no effect', () => {
	const { state, tick, stop } = armWithFakes({ playing: true });
	stop();
	state.elevated = true;
	tick(); // no-op: the driver's listener was already unsubscribed by stop()
	assert.equal(mod.pressureScaledPrefetchByteCap(STANDARD_BYTES), STANDARD_BYTES);
});
