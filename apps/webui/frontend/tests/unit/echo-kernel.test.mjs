// requirement: FXECHO-03
// real-audio counterpart (Chromium OfflineAudioContext): tests/e2e/echo-retune-real-audio.spec.ts
// [if] the division changes 1/4 to 1 beat at 124 BPM [then] the new line is stepped silent and crossfaded in over 30 ms
// [if] a retune arrives mid-crossfade [then] the two line gains sum to 1 within 0.01 at every render quantum
// [if] the echo gets its first time [then] it is set as a step
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FORBIDDEN_ON_DELAY = new Set(['target', 'linear', 'exponential', 'curve', 'hold']);

/** Records automation events and evaluates valueAt for tests. */
class AutoParam {
	constructor(value = 0) {
		this.value = value;
		this.events = [];
		this._calls = [];
	}
	cancelScheduledValues(t) {
		this.events = this.events.filter((e) => e[2] < t);
	}
	setValueAtTime(v, t) {
		this.events.push(['set', v, t]);
		this._calls.push(['setValueAtTime', v, t]);
	}
	linearRampToValueAtTime(v, t) {
		this.events.push(['linear', v, t]);
		this._calls.push(['linearRampToValueAtTime', v, t]);
	}
	setTargetAtTime(v, t, tau) {
		this.events.push(['target', v, t, tau]);
		this._calls.push(['setTargetAtTime', v, t, tau]);
	}
	exponentialRampToValueAtTime(v, t) {
		this.events.push(['exponential', v, t]);
		this._calls.push(['exponentialRampToValueAtTime', v, t]);
	}
	setValueCurveAtTime(curve, t, duration) {
		this.events.push(['curve', curve, t, duration]);
		this._calls.push(['setValueCurveAtTime', curve, t, duration]);
	}
	cancelAndHoldAtTime(t) {
		this.events.push(['hold', t]);
		this._calls.push(['cancelAndHoldAtTime', t]);
	}
	valueAt(t) {
		let val = this.value;
		let lastT = -Infinity;
		const sorted = [...this.events].sort((a, b) => a[2] - b[2] || (a[0] === 'set' ? -1 : 1));
		for (const e of sorted) {
			if (e[0] === 'set') {
				const et = e[2];
				if (et > t) continue;
				val = e[1];
				lastT = et;
			} else if (e[0] === 'linear') {
				if (lastT === -Infinity) continue;
				const tEnd = e[2];
				const vEnd = e[1];
				if (tEnd <= t) {
					val = vEnd;
					lastT = tEnd;
				} else if (t >= lastT) {
					const span = tEnd - lastT;
					const frac = Math.min(1, Math.max(0, (t - lastT) / span));
					return val + (vEnd - val) * frac;
				}
			} else if (FORBIDDEN_ON_DELAY.has(e[0])) {
				throw new Error(`unexpected ${e[0]} in timeline`);
			}
		}
		return val;
	}
}

class FakeNode {
	constructor(kind, ctx) {
		this.kind = kind;
		Object.defineProperty(this, 'context', { value: ctx, enumerable: false });
		this.outputs = new Set();
	}
	connect(node) {
		this.outputs.add(node);
		return node;
	}
}

function fakeContext() {
	const ctx = {
		currentTime: 10,
		sampleRate: 48000,
		_node(kind) {
			const n = new FakeNode(kind, ctx);
			ctx.created.push(n);
			return n;
		},
		createGain() {
			const n = ctx._node('gain');
			n.gain = new AutoParam(1);
			return n;
		},
		createDelay(max) {
			const n = ctx._node('delay');
			n.maxDelayTime = max;
			n.delayTime = new AutoParam(0);
			return n;
		},
		createBiquadFilter() {
			const n = ctx._node('biquad');
			n.type = 'lowpass';
			n.frequency = new AutoParam(350);
			return n;
		},
		createWaveShaper() {
			const n = ctx._node('waveshaper');
			n.curve = null;
			n.oversample = 'none';
			return n;
		}
	};
	Object.defineProperty(ctx, 'created', { value: [], enumerable: false });
	return ctx;
}

describe('echo-kernel', async () => {
	const kernel = await loadTypeScriptModule('src/lib/rb/fx/echo-kernel.ts');
	const { buildEchoKernel, ECHO_RETUNE_XFADE_SEC } = kernel;
	const MAX = 1.5;
	const OPTS = { maxDelaySec: MAX, feedback: 0.55, loopHpHz: 180, loopLpHz: 3000 };

	test('topology: two delays, wiring, line gains 1 and 0, setTime allocates nothing', () => {
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		const delays = ctx.created.filter((n) => n.kind === 'delay');
		assert.equal(delays.length, 2);
		for (const d of delays) assert.equal(d.maxDelayTime, MAX);
		assert.deepEqual([...k.input.outputs], [k.softClip]);
		assert.ok(k.softClip.outputs.has(k.lines[0]) && k.softClip.outputs.has(k.lines[1]));
		assert.equal(ctx.created.filter((n) => n.kind === 'waveshaper').length, 1);
		assert.equal(k.softClip.curve.length, kernel.ECHO_LOOP_CURVE_LENGTH);
		assert.equal(k.softClip.oversample, 'none');
		assert.ok(k.nodes.includes(k.softClip));
		const curve = k.softClip.curve;
		assert.ok(k.lines[0].outputs.has(k.lineGains[0]) && !k.lines[0].outputs.has(k.lineGains[1]));
		assert.ok(k.lines[1].outputs.has(k.lineGains[1]));
		assert.ok(k.lineGains[0].outputs.has(k.output) && k.lineGains[1].outputs.has(k.output));
		assert.ok(k.output.outputs.has(k.loopHp));
		assert.ok(k.loopHp.outputs.has(k.loopLp));
		assert.ok(k.loopLp.outputs.has(k.feedback));
		assert.ok(k.feedback.outputs.has(k.input));
		assert.equal(k.lineGains[0].gain.value, 1);
		assert.equal(k.lineGains[1].gain.value, 0);
		const before = ctx.created.length;
		k.setTime(0.3, 10);
		k.setTime(0.4, 11);
		assert.equal(ctx.created.length, before);
		assert.equal(k.softClip.curve, curve);
	});

	test('soft clip curve: identity to the knee, odd, monotonic, under the ceiling, smooth knee', () => {
		const { ECHO_LOOP_KNEE, ECHO_LOOP_CEILING, echoLoopSoftClipCurve } = kernel;
		const c = echoLoopSoftClipCurve();
		const n = c.length;
		const step = 2 / (n - 1);
		for (let k = 0; k < n; k++) {
			const x = k * step - 1;
			if (Math.abs(x) <= ECHO_LOOP_KNEE) assert.equal(c[k], Math.fround(x), `identity at ${x}`);
			assert.equal(c[k] + c[n - 1 - k], 0, `odd at ${x}`);
			if (k > 0) assert.ok(c[k] >= c[k - 1], `monotonic at ${x}`);
			assert.ok(Math.abs(c[k]) < ECHO_LOOP_CEILING, `under ceiling at ${x}`);
		}
		const kneeIdx = Math.floor((ECHO_LOOP_KNEE + 1) / step);
		assert.ok(Math.abs(c[kneeIdx + 2] - c[kneeIdx + 1] - step) < 1e-3);
		assert.ok(Math.abs(c[kneeIdx] - c[kneeIdx - 1] - step) < 1e-3);
	});

	test('cold start is a step', () => {
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		k.setTime(0.3515625, 10);
		assert.deepEqual(k.lines[0].delayTime.events, [['set', 0.3515625, 10]]);
		assert.equal(k.lines[1].delayTime.events.length, 0);
		assert.equal(k.lineGains[0].gain.events.length, 0);
		assert.equal(k.lineGains[1].gain.events.length, 0);
		assert.equal(k.time(), 0.3515625);
	});

	test('warm retune crossfades over ECHO_RETUNE_XFADE_SEC', () => {
		assert.equal(ECHO_RETUNE_XFADE_SEC, 0.03);
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		k.setTime(0.3515625, 10);
		const oldEvents = k.lines[0].delayTime.events.length;
		k.setTime(0.484, 11);
		assert.deepEqual(k.lines[1].delayTime.events, [['set', 0.484, 11]]);
		assert.equal(k.lineGains[1].gain.valueAt(11), 0);
		for (const g of k.lineGains) {
			const kinds = g.gain.events.map((e) => e[0]);
			assert.ok(kinds.includes('set') && kinds.includes('linear'));
			const linear = g.gain.events.find((e) => e[0] === 'linear');
			assert.equal(linear[2], 11 + ECHO_RETUNE_XFADE_SEC);
		}
		assert.equal(k.lineGains[0].gain.valueAt(11 + ECHO_RETUNE_XFADE_SEC), 0);
		assert.equal(k.lineGains[1].gain.valueAt(11 + ECHO_RETUNE_XFADE_SEC), 1);
		assert.equal(k.lines[0].delayTime.events.length, oldEvents);
	});

	test('no ramp on any delayTime', () => {
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		k.setTime(0.12, 10);
		k.setTime(0.24, 11);
		k.setTime(0.36, 11.012);
		k.setTime(0.48, 11.02);
		k.setTime(0.48, 11.5);
		k.setTime(0.6, 12);
		for (const line of k.lines) {
			for (const e of line.delayTime.events) {
				assert.equal(e[0], 'set', `delayTime must use set only: ${JSON.stringify(e)}`);
			}
			for (const c of line.delayTime._calls) {
				const name = c[0];
				if (name !== 'setValueAtTime' && name !== 'cancelScheduledValues') {
					assert.fail(`forbidden on delayTime: ${name}`);
				}
			}
		}
		let lineLinear = 0;
		for (const g of k.lineGains) {
			if (g.gain.events.some((e) => e[0] === 'linear')) lineLinear++;
		}
		assert.ok(lineLinear >= 2, 'line gains received linear ramps (control)');
		for (const line of k.lines) {
			for (const e of line.delayTime.events) {
				if (e[2] === 10) continue;
				const idx = line === k.lines[0] ? 0 : 1;
				assert.equal(k.lineGains[idx].gain.valueAt(e[2]), 0, 'warm set only when gain is 0');
			}
		}
	});

	test('mid-crossfade retune: gain sum and max delta per quantum', () => {
		const a = 0.12;
		const b = 0.24;
		const c = 0.36;
		const d = 0.48;
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		k.setTime(a, 10);
		k.setTime(b, 11);
		k.setTime(c, 11.012);
		k.setTime(d, 11.02);
		// 0.01-per-quantum: summed gain within 0.01 of 1; no jump beyond the 30 ms ramp slope.
		const quantum = 128 / 48000;
		const maxDelta = quantum / ECHO_RETUNE_XFADE_SEC + 0.01;
		const tEnd = 11.2;
		// Sample from the first warm retune onward so quanta do not span a fade's setValueAtTime boundary.
		let t = 11;
		let prev0 = k.lineGains[0].gain.valueAt(t);
		let prev1 = k.lineGains[1].gain.valueAt(t);
		t += quantum;
		while (t <= tEnd) {
			const g0 = k.lineGains[0].gain.valueAt(t);
			const g1 = k.lineGains[1].gain.valueAt(t);
			assert.ok(Math.abs(g0 + g1 - 1) <= 0.01, `sum at ${t}: ${g0}+${g1}`);
			assert.ok(Math.abs(g0 - prev0) <= maxDelta);
			assert.ok(Math.abs(g1 - prev1) <= maxDelta);
			prev0 = g0;
			prev1 = g1;
			t += quantum;
		}
		const liveIdx = k.lineGains[1].gain.valueAt(11.2) >= 0.5 ? 1 : 0;
		assert.equal(k.lines[liveIdx].delayTime.valueAt(11.2), d);
		assert.equal(k.lineGains[1 - liveIdx].gain.valueAt(11.2), 0);
		assert.equal(k.time(), d);
	});

	test('replacing back to in-progress target cancels the queue', () => {
		const a = 0.12;
		const b = 0.24;
		const c = 0.36;
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		k.setTime(a, 10);
		k.setTime(b, 11);
		k.setTime(c, 11.01);
		k.setTime(b, 11.015);
		assert.equal(k.time(), b);
		assert.equal(
			k.lines[1].delayTime.events.every((e) => e[1] !== c),
			true
		);
		assert.equal(k.lineGains[1].gain.valueAt(11.03), 1);
		assert.equal(k.lineGains[0].gain.valueAt(11.03), 0);
	});

	test('unchanged time is a no-op', () => {
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		k.setTime(0.5, 10);
		const n0 = k.lines[0].delayTime.events.length;
		const ng0 = k.lineGains[0].gain.events.length;
		k.setTime(0.5, 11);
		assert.equal(k.lines[0].delayTime.events.length, n0);
		assert.equal(k.lineGains[0].gain.events.length, ng0);
	});

	test('fail-fast RangeError leaves state untouched', () => {
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		k.setTime(0.5, 10);
		const bad = [NaN, Infinity, 0, -0.1, MAX + 0.001];
		for (const v of bad) {
			const evBefore = k.lines[0].delayTime.events.length;
			assert.throws(
				() => k.setTime(v, 12),
				(e) => e instanceof RangeError && /outside/.test(e.message)
			);
			assert.equal(k.lines[0].delayTime.events.length, evBefore);
			assert.equal(k.time(), 0.5);
		}
	});
});
