// requirement: FXECHO-03
// real-audio counterpart (Chromium OfflineAudioContext): tests/e2e/echo-retune-real-audio.spec.ts
// [if] the division changes 1/4 to 1 beat at 124 BPM [then] the new line is stepped silent and crossfaded in over 30 ms
// [if] a retune arrives mid-crossfade [then] the two line gains sum to 1 within 0.01 at every render quantum
// [if] the echo gets its first time [then] it is set as a step
// requirement: FXECHO-13830
// requirement: FXECHO-13945
// requirement: FXECHO-14147
// requirement: FXECHO-14240
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
		this.ports = [];
	}
	connect(node, output = 0, input = 0) {
		this.outputs.add(node);
		this.ports.push([node, output, input]);
		return node;
	}
	disconnect() {
		this.outputs.clear();
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
		},
		createChannelMerger(inputs) {
			return Object.assign(ctx._node('merger'), { inputs });
		}
	};
	Object.defineProperty(ctx, 'created', { value: [], enumerable: false });
	return ctx;
}

describe('echo-kernel', async () => {
	const kernel = await loadTypeScriptModule('src/lib/rb/fx/echo-kernel.ts');
	const { buildEchoKernel, ECHO_RETUNE_XFADE_SEC } = kernel;
	const MAX = 1.5;
	const OPTS = { mode: 'echo', maxDelaySec: MAX, feedback: 0.55, loopHpHz: 180, loopLpHz: 3000 };

	test('topology: two delays, wiring, line gains 1 and 0, setTime allocates nothing', () => {
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		const delays = ctx.created.filter((n) => n.kind === 'delay' && n !== k.pong);
		assert.equal(delays.length, 2);
		for (const d of delays) assert.equal(d.maxDelayTime, MAX);
		assert.deepEqual([...k.loopSum.outputs], [k.softClip]);
		assert.deepEqual([...k.softClip.outputs], [k.clipMakeup]);
		assert.ok(k.clipMakeup.outputs.has(k.lines[0]) && k.clipMakeup.outputs.has(k.lines[1]));
		assert.equal(ctx.created.filter((n) => n.kind === 'waveshaper').length, 1);
		assert.equal(k.softClip.curve.length, kernel.ECHO_LOOP_CURVE_LENGTH);
		assert.equal(k.softClip.oversample, 'none');
		assert.ok(k.nodes.includes(k.softClip));
		const curve = k.softClip.curve;
		assert.ok(k.lines[0].outputs.has(k.lineGains[0]) && !k.lines[0].outputs.has(k.lineGains[1]));
		assert.ok(k.lines[1].outputs.has(k.lineGains[1]));
		assert.ok(k.lineGains[0].outputs.has(k.lineSum) && k.lineGains[1].outputs.has(k.lineSum));
		assert.ok(k.lineSum.outputs.has(k.loopHp));
		assert.ok(k.loopHp.outputs.has(k.loopLp));
		assert.ok(k.loopLp.outputs.has(k.feedback));
		assert.ok(k.feedback.outputs.has(k.loopSum));
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

describe('echo-kernel modes', async () => {
	const kernel = await loadTypeScriptModule('src/lib/rb/fx/echo-kernel.ts');
	const { buildEchoKernel, ECHO_GATE_RAMP_SEC, ECHO_LINE_CEILING_SEC } = kernel;
	const OPTS = {
		mode: 'echo',
		maxDelaySec: ECHO_LINE_CEILING_SEC,
		feedback: 0.55,
		loopHpHz: 180,
		loopLpHz: 3000
	};
	const ALLOWED = new Set(['delay', 'biquad', 'gain', 'merger', 'waveshaper']);

	// [if] the kernel is built or driven through every setter [then] it has the full API, only native kinds, and no node is created later, [else stop].
	test('surface: API, native node kinds, nothing created after construction', () => {
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		for (const fn of [
			'setTime',
			'setFeedback',
			'setMode',
			'gateInput',
			'setHold',
			'setWidth',
			'dispose'
		])
			assert.equal(typeof k[fn], 'function');
		assert.ok(k.input && k.output);
		assert.deepEqual([...k.input.outputs], [k.loopSum]);
		for (const n of ctx.created) assert.ok(ALLOWED.has(n.kind), `unexpected ${n.kind}`);
		assert.equal(ctx.created.length, k.nodes.length);
		const before = ctx.created.length;
		k.setTime(0.5, 10);
		for (const mode of ['delay', 'pingpong', 'tape', 'echo']) k.setMode(mode, 11);
		k.setFeedback(0.7, 12);
		k.gateInput(false, 13);
		k.gateInput(true, 14);
		k.setTime(0.25, 15);
		for (const level of [0.5, 1, 0]) k.setHold(level, 16);
		k.setMode('pingpong', 17);
		k.setWidth(0.5, 18);
		assert.equal(ctx.created.length, before);
	});

	// [if] DELAY, ECHO or PING PONG is built [then] the loop gain, loop filters and L/R split match the mode, [else stop].
	test('modes: delay forces loop 0, echo filters the loop, pingpong splits L and R at half the time', () => {
		const d = buildEchoKernel(fakeContext(), { ...OPTS, mode: 'delay' });
		assert.equal(d.feedback.gain.value, 0);
		assert.equal(d.mode(), 'delay');
		d.setFeedback(0.8, 10);
		assert.equal(d.feedback.gain.events.length, 0, 'delay mode keeps the loop at 0');

		const e = buildEchoKernel(fakeContext(), OPTS);
		assert.equal(e.feedback.gain.value, 0.55);
		const tone = [e.loopHp, e.loopLp].map((f) => `${f.type} ${f.frequency.value}`);
		assert.deepEqual(tone, ['highpass 180', 'lowpass 3000']);
		assert.deepEqual([e.wet.gain.value, e.pingWet.gain.value], [1, 0]);

		const p = buildEchoKernel(fakeContext(), { ...OPTS, mode: 'pingpong' });
		assert.deepEqual([p.wet.gain.value, p.pingWet.gain.value, p.feedback.gain.value], [0, 1, 0.55]);
		p.setTime(0.5, 10);
		assert.deepEqual(p.lines[0].delayTime.events, [['set', 0.5, 10]]);
		assert.deepEqual(p.pong.delayTime.events, [['set', 0.25, 10]]);
		const merger = [...p.straight[0].outputs][0];
		assert.equal(merger.kind, 'merger');
		const port = (from) => from.ports.find(([n]) => n === merger).slice(1);
		const mono = [...p.lineSum.outputs].find((n) => n.outputs.has(p.pong));
		assert.equal(mono, p.mono);
		assert.ok(mono.outputs.has(p.straight[0]) && mono.outputs.has(p.cross[1]));
		assert.ok(p.pongGain.outputs.has(p.straight[1]) && p.pongGain.outputs.has(p.cross[0]));
		const ports = JSON.stringify([...p.straight, ...p.cross].map(port));
		assert.equal(ports, '[[0,0],[0,1],[0,0],[0,1]]', 'own line to its side, cross to the other');
		assert.equal(mono.channelCount, 1);

		e.setMode('pingpong', 20);
		assert.equal(e.wet.gain.valueAt(20 + ECHO_GATE_RAMP_SEC), 0);
		assert.equal(e.pingWet.gain.valueAt(20 + ECHO_GATE_RAMP_SEC), 1);
		e.setMode('delay', 30);
		assert.equal(e.feedback.gain.valueAt(30 + ECHO_GATE_RAMP_SEC), 0);
		e.setMode('echo', 40);
		assert.equal(e.feedback.gain.valueAt(40 + ECHO_GATE_RAMP_SEC), 0.55);
		assert.throws(() => e.setMode('warp', 50), /warp is not one of delay, echo, pingpong, tape/);
	});

	// [if] TAPE retunes [then] the live line glides by setTargetAtTime at 120 ms (or tapeGlideSec) with no line crossfade, [else stop].
	test('tape: a retune glides delayTime at ECHO_TAPE_GLIDE_SEC or the tapeGlideSec override', () => {
		assert.equal(kernel.ECHO_TAPE_GLIDE_SEC, 0.12);
		for (const [extra, tau] of [
			[{}, 0.12],
			[{ tapeGlideSec: 0.25 }, 0.25]
		]) {
			const k = buildEchoKernel(fakeContext(), { ...OPTS, ...extra, mode: 'tape' });
			k.setTime(0.5, 10);
			k.setTime(0.25, 11);
			assert.deepEqual(k.lines[0].delayTime.events.at(-1), ['target', 0.25, 11, tau]);
			assert.deepEqual(k.pong.delayTime.events.at(-1), ['target', 0.125, 11, tau]);
			assert.equal(k.lines[1].delayTime.events.length, 0, 'no second line in a glide');
			for (const g of k.lineGains) assert.equal(g.gain.events.length, 0, 'no crossfade');
			assert.equal(k.time(), 0.25);
		}
		for (const bad of [0, -0.1, NaN, Infinity]) {
			assert.throws(
				() => buildEchoKernel(fakeContext(), { ...OPTS, tapeGlideSec: bad }),
				(err) => err instanceof RangeError && /tape glide/.test(err.message)
			);
		}
		const e = buildEchoKernel(fakeContext(), OPTS);
		e.setTime(0.5, 10);
		e.setMode('tape', 11);
		e.setTime(0.4, 12);
		e.setMode('echo', 13);
		e.setTime(0.3, 14);
		assert.deepEqual(e.lines[1].delayTime.events, [['set', 0.3, 14]], 'echo again crossfades');
	});

	// [if] TAPE is the mode [then] the loop is HP only and the clip is driven 6 dB with the same makeup after it, [else stop].
	test('tape: HP-only loop and a soft clip driven 6 dB harder with makeup', () => {
		const { ECHO_TAPE_DRIVE, ECHO_TAPE_DRIVE_DB } = kernel;
		assert.equal(ECHO_TAPE_DRIVE_DB, 6);
		assert.ok(Math.abs(ECHO_TAPE_DRIVE - 10 ** (6 / 20)) < 1e-12);
		const t = buildEchoKernel(fakeContext(), { ...OPTS, mode: 'tape' });
		assert.ok(t.loopHp.outputs.has(t.tapeFeedback) && !t.loopLp.outputs.has(t.tapeFeedback));
		assert.ok(t.tapeFeedback.outputs.has(t.loopSum));
		assert.deepEqual([t.feedback.gain.value, t.tapeFeedback.gain.value], [0, 0.55]);
		assert.equal(t.loopSum.gain.value, ECHO_TAPE_DRIVE);
		assert.ok(Math.abs(t.loopSum.gain.value * t.clipMakeup.gain.value - 1) < 1e-12);
		t.setFeedback(0.8, 10);
		assert.equal(t.tapeFeedback.gain.valueAt(10 + ECHO_GATE_RAMP_SEC), 0.8);
		assert.equal(t.feedback.gain.events.length, 0, 'the LP path stays at 0');

		const e = buildEchoKernel(fakeContext(), OPTS);
		assert.deepEqual(
			[e.tapeFeedback.gain.value, e.loopSum.gain.value, e.clipMakeup.gain.value],
			[0, 1, 1]
		);
		e.setMode('tape', 20);
		const end = 20 + ECHO_GATE_RAMP_SEC;
		assert.deepEqual(e.loopSum.gain.events.at(-1), ['exponential', ECHO_TAPE_DRIVE, end]);
		assert.deepEqual(e.clipMakeup.gain.events.at(-1), ['exponential', 1 / ECHO_TAPE_DRIVE, end]);
		assert.deepEqual([e.feedback.gain.valueAt(end), e.tapeFeedback.gain.valueAt(end)], [0, 0.55]);
		e.setMode('echo', 30);
		assert.deepEqual(e.loopSum.gain.events.at(-1), ['exponential', 1, 30 + ECHO_GATE_RAMP_SEC]);
		assert.deepEqual([e.feedback.gain.valueAt(31), e.tapeFeedback.gain.valueAt(31)], [0.55, 0]);
	});

	// [if] feedback leaves [0, 0.95] or a time leaves one quantum..4000 ms [then] it throws naming value and limit, state untouched, [else stop].
	test('limits: feedback clamp and time range throw with the value and the limit', () => {
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		for (const bad of [-0.01, 0.951, 1, NaN, Infinity]) {
			assert.throws(
				() => k.setFeedback(bad, 10),
				(err) =>
					err instanceof RangeError && err.message.includes(`${bad}`) && /0\.95/.test(err.message)
			);
		}
		assert.equal(k.feedback.gain.events.length, 0);
		k.setFeedback(0.95, 10);
		assert.equal(k.feedback.gain.valueAt(10 + ECHO_GATE_RAMP_SEC), 0.95);
		assert.throws(
			() => buildEchoKernel(fakeContext(), { ...OPTS, feedback: 1.2 }),
			/echo feedback 1.2 is outside/
		);
		k.setTime(ECHO_LINE_CEILING_SEC, 10);
		k.setTime(128 / ctx.sampleRate, 11);
		assert.throws(() => k.setTime(4.001, 12), /echo time 4001 ms is outside .* 4000 ms line/);
		assert.throws(
			() => k.setTime(127 / ctx.sampleRate, 12),
			/2\.6 ms is outside the 2\.7 ms render quantum/
		);
		assert.equal(k.time(), 128 / ctx.sampleRate);
	});

	// [if] the input gate shuts [then] only input.gain ramps to 0 over 10 ms and every loop and output gain is untouched (trails), [else stop].
	test('gateInput(false) ramps only the input', () => {
		const k = buildEchoKernel(fakeContext(), OPTS);
		k.setTime(0.5, 10);
		const others = [k.feedback, k.wet, k.pingWet, k.output, k.lineSum, k.loopSum, ...k.lineGains];
		const snapshot = others.map((n) => JSON.stringify([n.gain.value, n.gain.events]));
		k.gateInput(false, 11);
		assert.equal(k.input.gain.valueAt(11), 1);
		assert.ok(Math.abs(k.input.gain.valueAt(11 + ECHO_GATE_RAMP_SEC / 2) - 0.5) < 1e-9);
		assert.equal(k.input.gain.valueAt(11 + ECHO_GATE_RAMP_SEC), 0);
		assert.deepEqual(
			others.map((n) => JSON.stringify([n.gain.value, n.gain.events])),
			snapshot
		);
		k.gateInput(true, 12);
		assert.equal(k.input.gain.events.at(-1)[1], 1);
	});

	// [if] the kernel is disposed [then] every node it built is disconnected and any later call throws, [else stop].
	test('dispose disconnects every node and refuses later calls', () => {
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		k.dispose();
		for (const n of ctx.created) assert.equal(n.outputs.size, 0, `${n.kind} still connected`);
		assert.throws(() => k.setTime(0.5, 10), /disposed/);
		assert.throws(() => k.gateInput(true, 10), /disposed/);
	});
});

describe('echo-kernel hold', async () => {
	const kernel = await loadTypeScriptModule('src/lib/rb/fx/echo-kernel.ts');
	const { buildEchoKernel, ECHO_GATE_RAMP_SEC, HELIX_HOLD_FEEDBACK } = kernel;
	const OPTS = { mode: 'echo', maxDelaySec: 2, feedback: 0.55, loopHpHz: 180, loopLpHz: 3000 };
	const R = ECHO_GATE_RAMP_SEC;
	const gains = (k, t) =>
		[k.input, k.feedback, k.tapeFeedback, k.holdFeedback].map((n) => n.gain.valueAt(t));
	const close = (got, want) => got.every((v, i) => Math.abs(v - want[i]) < 1e-12);

	// [if] setHold rises 0, 0.5, 1 [then] loop gain goes 0.55 to unity linearly on 10 ms ramps and the input shuts at 1; bad levels throw, [else stop].
	test('setHold law: mode feedback to HELIX_HOLD_FEEDBACK, input gate shut at 1, range checked', () => {
		assert.equal(HELIX_HOLD_FEEDBACK, 1);
		const k = buildEchoKernel(fakeContext(), OPTS);
		k.setHold(0, 10);
		assert.ok(close(gains(k, 10 + R), [1, 0.55, 0, 0]), 'hold 0: input open, mode feedback');
		k.setHold(0.5, 11);
		assert.ok(Math.abs(k.feedback.gain.valueAt(11 + R / 2) - (0.55 + 0.775) / 2) < 1e-12);
		assert.ok(close(gains(k, 11 + R), [1, 0.775, 0, 0]), 'hold 0.5: halfway to unity');
		k.setHold(1, 12);
		assert.ok(close(gains(k, 12 + R), [0, 0, 0, HELIX_HOLD_FEEDBACK]), 'hold 1: input shut');
		const t = buildEchoKernel(fakeContext(), { ...OPTS, mode: 'tape' });
		t.setHold(0.5, 10);
		assert.ok(close(gains(t, 10 + R), [1, 0, 0.775, 0]), 'tape: the HP-only path carries it');
		for (const bad of [-0.01, 1.01, NaN, Infinity]) {
			const before = k.input.gain.events.length;
			assert.throws(
				() => k.setHold(bad, 13),
				(err) => err instanceof RangeError && err.message === `echo hold ${bad} is outside [0, 1]`
			);
			assert.equal(k.input.gain.events.length, before, 'state untouched');
		}
	});

	// [if] hold reaches 1 and drops back [then] a built-once straight gain past both filters crossfades with the filtered loop gain over 10 ms, [else stop].
	test('hold 1 crossfades the loop filter out on a straight path built once, and back below 1', () => {
		const ctx = fakeContext();
		const k = buildEchoKernel(ctx, OPTS);
		assert.ok(k.lineSum.outputs.has(k.holdFeedback) && k.holdFeedback.outputs.has(k.loopSum));
		assert.ok(!k.loopHp.outputs.has(k.holdFeedback) && !k.loopLp.outputs.has(k.holdFeedback));
		assert.equal(k.holdFeedback.gain.value, 0);
		const before = ctx.created.length;
		k.setHold(0.999, 10);
		for (const [level, at, want] of [
			[1, 11, [0, 1]],
			[0.5, 12, [0.775, 0]]
		]) {
			k.setHold(level, at);
			for (const [n, v] of [k.feedback, k.holdFeedback].map((node, i) => [node, want[i]])) {
				const [kind, end, t] = n.gain.events.at(-1);
				assert.ok(kind === 'linear' && Math.abs(end - v) < 1e-12 && t === at + R, `${end} at ${t}`);
			}
			// Both ramps start at `at` (this fake's value is static, so the render test checks the fade).
			for (const n of [k.feedback, k.holdFeedback]) assert.equal(n.gain.events.at(-2)[2], at);
		}
		assert.equal(ctx.created.length, before, 'no node built after construction');
	});

	// [if] setFeedback(1.0) is called, held or not [then] it throws with the 0.95 clamp and only the hold law reaches unity, [else stop].
	test('setFeedback keeps its clamp: 1.0 throws, held or not', () => {
		const k = buildEchoKernel(fakeContext(), OPTS);
		const refuse = () =>
			assert.throws(
				() => k.setFeedback(1.0, 10),
				(err) => /1 is outside \[0, 0\.95\]/.test(err.message)
			);
		refuse();
		k.setHold(1, 11);
		refuse();
		k.setFeedback(0.7, 12);
		assert.equal(k.feedback.gain.valueAt(12 + R), 0, 'held: the filtered path stays out');
		k.setHold(0, 13);
		assert.equal(k.feedback.gain.valueAt(13 + R), 0.7, 'release lands on the new mode value');
	});

	// [if] hold returns to 0 [then] the input reopens unless gateInput shut it, the mode feedback returns and the straight path is 0, [else stop].
	test('release reopens the input, restores the mode feedback, and respects the gate', () => {
		const k = buildEchoKernel(fakeContext(), OPTS);
		k.setHold(1, 10);
		k.gateInput(true, 11);
		assert.equal(k.input.gain.valueAt(11 + R), 0, 'held: gateInput(true) keeps it shut');
		k.setHold(0, 12);
		assert.ok(close(gains(k, 12 + R), [1, 0.55, 0, 0]));
		k.gateInput(false, 13);
		k.setHold(1, 14);
		k.setHold(0, 15);
		assert.equal(k.input.gain.valueAt(15 + R), 0, 'a shut gate stays shut after release');
	});

	// [if] the time changes while held [then] the live line glides at the tape constant by default and crossfades lines with holdGlide false, [else stop].
	test('retune while held: glide by default, crossfade with holdGlide false', () => {
		const g = buildEchoKernel(fakeContext(), { ...OPTS, tapeGlideSec: 0.2 });
		g.setTime(0.5, 10);
		g.setHold(1, 11);
		g.setTime(0.25, 12);
		assert.deepEqual(g.lines[0].delayTime.events.at(-1), ['target', 0.25, 12, 0.2]);
		for (const n of g.lineGains) assert.equal(n.gain.events.length, 0, 'no crossfade');
		const x = buildEchoKernel(fakeContext(), { ...OPTS, holdGlide: false });
		x.setTime(0.5, 10);
		x.setHold(1, 11);
		x.setTime(0.25, 12);
		assert.deepEqual(x.lines[1].delayTime.events, [['set', 0.25, 12]]);
		assert.equal(x.lineGains[1].gain.valueAt(12 + kernel.ECHO_RETUNE_XFADE_SEC), 1);
		assert.ok(
			x.lines[0].delayTime.events.every((e) => e[0] === 'set'),
			'no glide'
		);
	});
});

describe('echo-kernel pingpong width', async () => {
	const kernel = await loadTypeScriptModule('src/lib/rb/fx/echo-kernel.ts');
	const { buildEchoKernel, ECHO_GATE_RAMP_SEC: R, PINGPONG_WIDTH_DEFAULT } = kernel;
	const { pingpongWidthMakeup } = kernel;
	const PLAIN = { mode: 'pingpong', maxDelaySec: 2, feedback: 0.55, loopHpHz: 180, loopLpHz: 3000 };
	// The equal-power makeup is the A/B page's opt-in; the width law tests run with it on.
	const OPTS = { ...PLAIN, pingpongMakeup: true };
	const mix = (k, t) => [...k.straight, ...k.cross].map((n) => n.gain.valueAt(t));
	const tail = (param, n) => param.events.slice(-n).map(([kind, v, t]) => [kind, v, t]);
	const stage = (k) => [k.mono, k.loopSum, k.clipMakeup].map((n) => n.gain.value);
	const widthEvents = (k) =>
		JSON.stringify([...k.straight, ...k.cross, k.mono].map((n) => n.gain.events));

	// [if] setWidth moves 1 to 0 to 0.5 [then] mix, offset and makeup follow the linear law on 10 ms ramps; bad widths throw with the value, [else stop].
	test('setWidth law: linear mix and offset, makeup with the drive, range checked', () => {
		const k = buildEchoKernel(fakeContext(), OPTS);
		k.setTime(0.5, 10);
		assert.deepEqual(mix(k, 10), [1, 1, 0, 0]);
		k.setWidth(0, 11);
		assert.deepEqual(mix(k, 11 + R), [0.5, 0.5, 0.5, 0.5]);
		assert.ok(Math.abs(mix(k, 11 + R / 2)[0] - 0.75) < 1e-12, 'linear ramp');
		assert.deepEqual(k.pong.delayTime.events.at(-1), ['set', 0, 11 + R / 2]);
		const duck = [0, R / 2, R].map((dt) => k.pongGain.gain.valueAt(11 + dt));
		assert.deepEqual(duck, [1, 0, 1]);
		assert.deepEqual(tail(k.mono.gain, 1), [['linear', 1, 11 + R]], 'makeup falls at once');
		assert.deepEqual(tail(k.loopSum.gain, 1), [['exponential', 1, 11 + R]]);
		k.setWidth(0.5, 12);
		const m = pingpongWidthMakeup(0.5);
		assert.deepEqual(mix(k, 12 + R), [0.75, 0.75, 0.25, 0.25]);
		assert.deepEqual(k.pong.delayTime.events.at(-1), ['set', 0.125, 12 + R / 2]);
		const rise = tail(k.mono.gain, 2).map(([kind, v, t]) => `${kind} ${v} ${t}`);
		assert.deepEqual(rise.slice(1), [`linear ${m} ${12.5 + R}`], 'makeup rise waits one line');
		assert.match(rise[0], /^set .* 12\.5$/);
		assert.deepEqual(tail(k.clipMakeup.gain, 1), [['exponential', 1 / m, 12 + R]]);
		const snapshot = widthEvents(k);
		for (const bad of [-0.01, 1.01, NaN, Infinity]) {
			assert.throws(
				() => k.setWidth(bad, 13),
				(err) => err instanceof RangeError && err.message.includes(`width ${bad} is outside [0, 1]`)
			);
		}
		assert.deepEqual([widthEvents(k), k.width()], [snapshot, 0.5]);
	});

	// [if] setWidth is called in DELAY, ECHO or TAPE [then] it throws naming the mode and the width is unchanged, [else stop].
	test('setWidth outside pingpong throws naming the mode', () => {
		for (const mode of ['delay', 'echo', 'tape']) {
			const k = buildEchoKernel(fakeContext(), { ...OPTS, mode });
			assert.throws(() => k.setWidth(0.5, 10), new RegExp(`needs pingpong mode, not ${mode}$`));
			assert.equal(k.width(), PINGPONG_WIDTH_DEFAULT);
		}
	});

	// [if] the kernel is built with or without options.pingpongWidth [then] it starts at the option or PINGPONG_WIDTH_DEFAULT 1, unity per side without the makeup, [else stop].
	test('PINGPONG_WIDTH_DEFAULT is 1 and options.pingpongWidth overrides it', () => {
		assert.equal(PINGPONG_WIDTH_DEFAULT, 1);
		const full = buildEchoKernel(fakeContext(), OPTS);
		assert.deepEqual([full.width(), full.mono.gain.value], [1, Math.SQRT2]);
		// No options: main's PING PONG, unity per side and an undriven loop, no 3 dB lift.
		const plain = buildEchoKernel(fakeContext(), PLAIN);
		assert.deepEqual([plain.width(), ...stage(plain), ...mix(plain, 0)], [1, 1, 1, 1, 1, 1, 0, 0]);
		const k = buildEchoKernel(fakeContext(), { ...OPTS, pingpongWidth: 0.5 });
		const m = pingpongWidthMakeup(0.5);
		assert.deepEqual(mix(k, 0), [0.75, 0.75, 0.25, 0.25]);
		assert.deepEqual(stage(k), [m, m, 1 / m]);
		k.setTime(0.5, 10);
		assert.deepEqual(k.pong.delayTime.events, [['set', 0.125, 10]]);
		for (const extra of [{ pingpongMakeup: false }, { mode: 'echo' }]) {
			const u = buildEchoKernel(fakeContext(), { ...OPTS, pingpongWidth: 0.5, ...extra });
			assert.deepEqual(stage(u), [1, 1, 1]);
		}
		for (const bad of [-1, 2, NaN]) {
			assert.throws(
				() => buildEchoKernel(fakeContext(), { ...OPTS, pingpongWidth: bad }),
				new RegExp(`echo width ${bad} is outside`)
			);
		}
	});
});
