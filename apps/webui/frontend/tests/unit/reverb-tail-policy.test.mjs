// requirement: FXVERB-14655
// requirement: FXVERB-14813
// [if] the tail table misses an event or differs from spec (load/eject cut, stop/pause/switch ring, teardown dispose) [then] broken, [else stop].
// [if] the cut steps the return to its resting gain before ramping, instead of ramping from its current gain [then] broken, [else stop].
// [if] a SPACE tail at track load is above -90 dB 50 ms after the cut, or the cut's click ratio exceeds 1.5 [then] broken, [else stop].
// [if] a tail at deck pause is touched, detached, or stops decaying on its own RT60 [then] broken, [else stop].
// Verified offline, no listening: the cut automation the policy schedules is evaluated sample by sample over the shipped SPACE impulse and a decaying 437 Hz tone.
import assert from 'node:assert/strict';
import { before, describe, test } from 'node:test';

import * as policy from '../../src/lib/rb/reverb-tail-policy.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const SR = 48000;
const TONE_HZ = 437;

let graph;

before(async () => {
	graph = await loadTypeScriptModule('src/lib/rb/color-fx-graph.ts');
});

class RecordingParam {
	constructor(value) {
		this.value = value;
		this.events = [];
	}
	cancelScheduledValues(t) {
		this.events = this.events.filter((e) => e.t < t);
	}
	setValueAtTime(v, t) {
		this.events.push({ kind: 'set', v, t });
	}
	linearRampToValueAtTime(v, t) {
		this.events.push({ kind: 'linear', v, t });
	}
	/** Web Audio's value at time t for set and linear events (spec 1.6.3 automation). */
	at(t) {
		let value = this.value;
		let prev = { t: -Infinity, v: this.value };
		for (const e of [...this.events].sort((a, b) => a.t - b.t)) {
			if (e.t > t) {
				if (e.kind === 'linear') return prev.v + ((e.v - prev.v) * (t - prev.t)) / (e.t - prev.t);
				return value;
			}
			value = e.v;
			prev = e;
		}
		return value;
	}
}

function fakeHost(restoreGain = 0.8) {
	const host = {
		returnGain: new RecordingParam(restoreGain),
		restoreGain,
		isAttached: true,
		attached: () => host.isAttached,
		detach: () => {
			host.isAttached = false;
		}
	};
	return host;
}

/** An AfterAudio that runs `done` at exactly now + delaySec, recording when. */
function manualClock(now) {
	const runs = [];
	return {
		runs,
		afterAudio: (delaySec, done) => runs.push(() => done(now + delaySec))
	};
}

/** The wet return's output: tail(t) times the return gain, and silence once detached. */
function renderThroughReturn(tail, played, detachAtSec) {
	const out = new Float32Array(tail.length);
	for (let n = 0; n < tail.length; n += 1) {
		const t = n / SR;
		out[n] = detachAtSec !== null && t >= detachAtSec ? 0 : tail[n] * played.at(t);
	}
	return out;
}

function decayingTone(seconds, rt60) {
	const out = new Float32Array(Math.round(seconds * SR));
	const k = Math.log(1000) / rt60;
	for (let n = 0; n < out.length; n += 1)
		out[n] = Math.sin((2 * Math.PI * TONE_HZ * n) / SR) * Math.exp((-k * n) / SR);
	return out;
}

function maxSlope(y, from, to) {
	let m = 0;
	for (let n = Math.max(1, from); n <= Math.min(y.length - 1, to); n += 1)
		m = Math.max(m, Math.abs(y[n] - y[n - 1]));
	return m;
}

/** Peak slope around the event over the larger of the steady slopes before and after (pitch-fx-kernel.test.mjs). */
function clickRatio(y, eventFrame) {
	const post = maxSlope(y, eventFrame - 128, eventFrame + 4096);
	const pre = maxSlope(y, eventFrame - Math.floor(0.3 * SR), eventFrame - Math.floor(0.1 * SR));
	const postSteady = maxSlope(
		y,
		eventFrame + Math.floor(0.25 * SR),
		eventFrame + Math.floor(0.45 * SR)
	);
	return post / Math.max(pre, postSteady, 1e-12);
}

function peakDb(y, from, to) {
	let p = 0;
	for (let n = from; n < to; n += 1) p = Math.max(p, Math.abs(y[n]));
	return 20 * Math.log10(Math.max(p, 1e-30));
}

function rmsDb(y, from, to) {
	let s = 0;
	for (let n = from; n < to; n += 1) s += y[n] * y[n];
	return 10 * Math.log10(Math.max(s / (to - from), 1e-30));
}

function spaceImpulseLeft() {
	const left = [];
	const createBuffer = () => ({ copyToChannel: (src, ch) => ch === 0 && left.push(src) });
	graph.spaceImpulseBuffer({ sampleRate: SR, createBuffer });
	return left[0];
}

describe('reverb tail policy', () => {
	test('one exhaustive table: load and eject cut, stop, pause and switch ring out, teardown disposes', () => {
		const spec = { track_load: 'cut', eject: 'cut', stop: 'ring_out', pause: 'ring_out' };
		Object.assign(spec, { effect_switch: 'ring_out', teardown: 'dispose' });
		assert.deepEqual({ ...policy.REVERB_TAIL_POLICY }, spec);
		assert.ok(Object.isFrozen(policy.REVERB_TAIL_POLICY));
		assert.equal(policy.REVERB_TAIL_CUT_SEC, 0.01);

		// cut: linear ramp to 0 over 10 ms, then detach, then the return is restored
		const cut = fakeHost();
		const clock = manualClock(5);
		assert.equal(policy.applyReverbTailEvent('track_load', cut, 5, clock.afterAudio), 'cut');
		assert.deepEqual(cut.returnGain.events, [
			{ kind: 'set', v: 0.8, t: 5 },
			{ kind: 'linear', v: 0, t: 5 + policy.REVERB_TAIL_CUT_SEC }
		]);
		assert.equal(cut.isAttached, true, 'not detached before the ramp lands');
		assert.equal(policy.applyReverbTailEvent('eject', cut, 5.004, clock.afterAudio), 'cut');
		assert.equal(clock.runs.length, 1, 'a second event inside the ramp joins the cut in flight');
		clock.runs[0]();
		assert.equal(cut.isAttached, false);
		assert.equal(cut.returnGain.at(5.0101), 0.8, 'return restored after the detach');

		// dispose detaches at once; ring out and a detached host touch nothing
		const torn = fakeHost();
		assert.equal(policy.applyReverbTailEvent('teardown', torn, 1, clock.afterAudio), 'dispose');
		assert.equal(torn.isAttached, false);
		for (const event of ['stop', 'pause', 'effect_switch']) {
			const ringing = fakeHost();
			assert.equal(policy.applyReverbTailEvent(event, ringing, 1, clock.afterAudio), 'ring_out');
			assert.equal(ringing.isAttached, true, `${event} keeps the tail`);
			assert.deepEqual(ringing.returnGain.events, [], `${event} never moves the return`);
		}
		const idle = fakeHost();
		idle.isAttached = false;
		assert.equal(policy.applyReverbTailEvent('track_load', idle, 1, clock.afterAudio), 'cut');
		assert.deepEqual(idle.returnGain.events, [], 'nothing attached, nothing to cut');
	});

	test("the cut ramps from the return's current gain, never stepping to its resting gain first", () => {
		const host = fakeHost(0.8);
		// The return sits elsewhere (mid-crossfade, or mid-restore of an earlier cut).
		host.returnGain.value = 0.25;
		const clock = manualClock(3);
		policy.applyReverbTailEvent('track_load', host, 3, clock.afterAudio);
		assert.deepEqual(host.returnGain.events, [
			{ kind: 'set', v: 0.25, t: 3 },
			{ kind: 'linear', v: 0, t: 3 + policy.REVERB_TAIL_CUT_SEC }
		]);
		// No step at the event: the value just after it is continuous with the value before.
		assert.ok(Math.abs(host.returnGain.at(3 + 1 / SR) - 0.25) < 0.25 / 400, 'no jump at the cut');
		clock.runs[0]();
		assert.equal(
			host.returnGain.at(3.0101),
			0.8,
			'restored to the resting gain only after the detach'
		);
	});

	test('a SPACE tail at track load is below -90 dB within 50 ms and the cut clicks at most 1.5', () => {
		const loadAt = 0.5;
		const ir = spaceImpulseLeft();
		const tone = decayingTone(1.2, graph.SPACE_RT60_SEC);
		for (const [name, tail] of [
			['SPACE impulse', ir.subarray(0, tone.length)],
			['437 Hz tone', tone]
		]) {
			const host = fakeHost(graph.SPACE_WET_GAIN);
			const clock = manualClock(loadAt);
			policy.applyReverbTailEvent('track_load', host, loadAt, clock.afterAudio);
			// Rendered from the automation as scheduled, before the post-detach restore replaces it.
			const out = renderThroughReturn(tail, host.returnGain, loadAt + policy.REVERB_TAIL_CUT_SEC);
			clock.runs[0]();
			assert.equal(host.isAttached, false, `${name}: detached once the ramp landed`);
			const loadFrame = Math.round(loadAt * SR);
			for (const v of out)
				assert.ok(Number.isFinite(v) && Math.abs(v) <= 1, `${name}: bounded and finite`);
			const before = peakDb(out, loadFrame - 2400, loadFrame);
			assert.ok(before > -30, `${name}: ringing at the load (control, ${before.toFixed(1)} dB)`);
			const after = peakDb(out, loadFrame + Math.round(0.05 * SR), out.length) - before;
			assert.ok(after < -90, `${name}: ${after.toFixed(1)} dB re the tail, 50 ms after load`);
			if (name === '437 Hz tone') {
				const ratio = clickRatio(out, loadFrame);
				assert.ok(ratio <= 1.5, `cut click ratio ${ratio.toFixed(3)}`);
				// Control: the same tail stepped to 0 with no ramp does click, so the ratio can see one.
				let peakFrame = loadFrame;
				for (let n = loadFrame; n < loadFrame + SR / TONE_HZ; n += 1)
					if (Math.abs(tone[n]) > Math.abs(tone[peakFrame])) peakFrame = n;
				const hard = Float32Array.from(tone, (v, n) =>
					n >= peakFrame ? 0 : v * graph.SPACE_WET_GAIN
				);
				assert.ok(clickRatio(hard, loadFrame) > 1.5, 'a hard cut measures as a click');
			}
		}
	});

	test('a tail at deck pause keeps decaying on its own RT60, never cut', () => {
		const host = fakeHost(graph.SPACE_WET_GAIN);
		const clock = manualClock(0.2);
		assert.equal(policy.applyReverbTailEvent('pause', host, 0.2, clock.afterAudio), 'ring_out');
		assert.equal(clock.runs.length, 0);
		assert.equal(host.isAttached, true);
		const ir = spaceImpulseLeft();
		const out = renderThroughReturn(ir, host.returnGain, null);
		// Decay rate between two windows well inside the tail: 60 dB over SPACE_RT60_SEC.
		const w = Math.round(0.1 * SR);
		const [t1, t2] = [0.3, 1.5];
		const early = rmsDb(out, Math.round(t1 * SR), Math.round(t1 * SR) + w);
		const late = rmsDb(out, Math.round(t2 * SR), Math.round(t2 * SR) + w);
		const rt60 = (60 * (t2 - t1)) / (early - late);
		assert.ok(Math.abs(rt60 - graph.SPACE_RT60_SEC) < 0.15, `measured RT60 ${rt60.toFixed(2)} s`);
		assert.ok(late > -120, 'still audible energy 1.5 s after the pause');
	});
});
