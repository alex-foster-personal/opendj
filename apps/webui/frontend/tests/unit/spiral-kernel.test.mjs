// requirement: FXECHO-14181
// Offline per-sample graph simulator (48 kHz, no listening): delay lines cut the cycles, RBJ biquads
// with the Web Audio dB Q, WaveShaper interpolation, set/linear/target automation per the spec.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SR = 48000;
const BEAT_SEC = 60 / 124;
const LOOP = { loopHpHz: 180, loopLpHz: 3000 };
const DB = (db) => 10 ** (db / 20);
const VARIANTS = ['light', 'medium', 'dense'];

class Param {
	constructor(clock, value) {
		this.clock = clock;
		this.base = value;
		this.events = [];
	}
	get value() {
		return this.valueAt(this.clock.t);
	}
	set value(v) {
		this.base = v;
	}
	_add(e) {
		this.events.push(e);
		this.events.sort((a, b) => a.t - b.t);
	}
	setValueAtTime(v, t) {
		this._add({ kind: 'set', v, t });
	}
	linearRampToValueAtTime(v, t) {
		this._add({ kind: 'linear', v, t });
	}
	setTargetAtTime(v, t, tau) {
		this._add({ kind: 'target', v, t, tau });
	}
	cancelScheduledValues(t) {
		this.events = this.events.filter((e) => e.t < t);
	}
	valueAt(t) {
		let val = this.base;
		let lastT = 0;
		let target = null;
		const at = (time) =>
			target ? target.v + (target.v0 - target.v) * Math.exp(-(time - target.t) / target.tau) : val;
		for (const e of this.events) {
			if (e.t > t) {
				if (e.kind === 'linear')
					return at(lastT) + ((e.v - at(lastT)) * (t - lastT)) / (e.t - lastT);
				break;
			}
			const before = at(e.t);
			target = e.kind === 'target' ? { ...e, v0: before } : null;
			val = e.kind === 'target' ? before : e.v;
			lastT = e.t;
		}
		return at(t);
	}
}

/** A context whose nodes record their wiring; `created` counts every factory call. */
function simContext() {
	const clock = { t: 0 };
	const ctx = { clock, sampleRate: SR, created: 0, all: [] };
	const node = (kind, extra) => {
		ctx.created += 1;
		const n = { kind, outs: [], disconnected: false, ...extra };
		n.connect = (dst) => (n.outs.push(dst), dst);
		n.disconnect = () => ((n.outs = []), (n.disconnected = true));
		ctx.all.push(n);
		return n;
	};
	ctx.createGain = () => node('gain', { gain: new Param(clock, 1) });
	ctx.createDelay = (max) =>
		node('delay', {
			max,
			delayTime: new Param(clock, 0),
			buf: new Float64Array(Math.ceil(max * SR) + 4)
		});
	ctx.createBiquadFilter = () =>
		node('biquad', { type: 'lowpass', frequency: new Param(clock, 350), Q: new Param(clock, 1) });
	ctx.createWaveShaper = () => node('waveshaper', { curve: null, oversample: 'none' });
	// Mono sim: a merger sums its inputs (the echo kernel's pingpong split only; muted in echo mode).
	ctx.createChannelMerger = (inputs) => node('merger', { inputs });
	return ctx;
}

function biquad(node) {
	const w0 = (2 * Math.PI * node.frequency.value) / SR;
	const alpha = Math.sin(w0) / (2 * 10 ** (node.Q.value / 20));
	const cos = Math.cos(w0);
	const lp = node.type === 'lowpass';
	const b0 = lp ? (1 - cos) / 2 : (1 + cos) / 2;
	const a0 = 1 + alpha;
	const c = [b0 / a0, (lp ? 1 - cos : -(1 + cos)) / a0, b0 / a0, (-2 * cos) / a0, (1 - alpha) / a0];
	const s = [0, 0, 0, 0];
	return (x) => {
		const y = c[0] * x + c[1] * s[0] + c[2] * s[1] - c[3] * s[2] - c[4] * s[3];
		s[1] = s[0];
		s[0] = x;
		s[3] = s[2];
		s[2] = y;
		return y;
	};
}

function shape(curve, x) {
	const v = ((curve.length - 1) / 2) * (x + 1);
	if (v <= 0) return curve[0];
	if (v >= curve.length - 1) return curve[curve.length - 1];
	const k = Math.floor(v);
	return (1 - (v - k)) * curve[k] + (v - k) * curve[k + 1];
}

/** Render `send(n)` from `from` to `to` for `seconds`; `at(n)` runs automation calls before sample n. */
function render(ctx, from, to, seconds, send, at = () => {}) {
	const live = ctx.all.filter((n) => !n.disconnected);
	const ins = new Map(live.map((n) => [n, []]));
	for (const n of live) for (const d of n.outs) ins.get(d).push(n);
	const order = [];
	const seen = new Set();
	const visit = (n) => {
		if (seen.has(n)) return;
		seen.add(n);
		if (n.kind !== 'delay') for (const src of ins.get(n)) visit(src);
		order.push(n);
	};
	live.forEach(visit);
	const filters = new Map(live.filter((n) => n.kind === 'biquad').map((n) => [n, biquad(n)]));
	const out = new Float64Array(Math.round(seconds * SR));
	const y = new Map();
	const sum = (n) => ins.get(n).reduce((acc, src) => acc + y.get(src), n === from ? ctx.input : 0);
	for (let i = 0; i < out.length; i++) {
		ctx.clock.t = i / SR;
		at(i);
		ctx.input = send(i);
		for (const n of order) {
			if (n.kind === 'delay') {
				const pos = i - n.delayTime.valueAt(ctx.clock.t) * SR;
				const k = Math.floor(pos);
				const read = (j) => (j < 0 ? 0 : n.buf[j % n.buf.length]);
				y.set(n, (1 - (pos - k)) * read(k) + (pos - k) * read(k + 1));
			} else if (n.kind === 'gain') y.set(n, sum(n) * n.gain.valueAt(ctx.clock.t));
			else if (n.kind === 'biquad') y.set(n, filters.get(n)(sum(n)));
			else if (n.kind === 'merger') y.set(n, sum(n));
			else if (n.kind === 'waveshaper') y.set(n, shape(n.curve, sum(n)));
			else throw new Error(`sim has no node kind ${n.kind}`);
		}
		for (const n of order) if (n.kind === 'delay') n.buf[i % n.buf.length] = sum(n);
		out[i] = y.get(to);
	}
	return out;
}

const impulse = (amp) => (i) => (i === 0 ? amp : 0);
const peak = (x, a, b) =>
	x.subarray(Math.round(a * SR), Math.round(b * SR)).reduce((m, v) => Math.max(m, Math.abs(v)), 0);
/** Normalized echo density (Abel and Huang 2006) over [1, 2) s: share of samples outside one RMS per 20 ms, over erfc(1/sqrt 2). */
const density = (x) => {
	const w = 0.02 * SR;
	let total = 0;
	for (let s = SR; s < 2 * SR; s += w) {
		const seg = x.subarray(s, s + w);
		const rms = Math.sqrt(seg.reduce((acc, v) => acc + v * v, 0) / w);
		total += seg.filter((v) => Math.abs(v) > rms).length / w / 0.3173;
	}
	return total / (SR / w);
};
/** Mean frequency (Hz) from upward zero crossings in [a, b) s. */
const frequency = (x, a, b) => {
	let first = -1;
	let last = -1;
	let count = 0;
	for (let i = Math.round(a * SR) + 1; i < Math.round(b * SR); i++) {
		if (x[i - 1] < 0 && x[i] >= 0) {
			if (first < 0) first = i;
			else count += 1;
			last = i;
		}
	}
	return (count * SR) / (last - first);
};

function vetoes(x, label) {
	for (let i = 0; i < x.length; i++) {
		const v = Math.abs(x[i]);
		assert.ok(Number.isFinite(v), `${label}: non-finite at ${i}`);
		assert.ok(v <= 1, `${label}: ${x[i]} over 0 dBFS at ${i}`);
		assert.ok(v === 0 || v >= 1.1754943508222875e-38, `${label}: subnormal at ${i}`);
	}
}

/** Impulse at 124 BPM, 1/2 beat, depth 0.7 through one variant. */
function spiralImpulse(m, variant, seconds) {
	const ctx = simContext();
	const k = m.buildSpiralKernel(ctx, { ...LOOP, depth: 0.7, spiralVariant: variant });
	k.setTime(BEAT_SEC / 2, 0);
	return render(ctx, k.input, k.output, seconds, impulse(0.5));
}

describe('SPIRAL kernel (#10003)', async () => {
	const m = await loadTypeScriptModule('src/lib/rb/fx/spiral-kernel.ts');
	const echo = await loadTypeScriptModule('src/lib/rb/fx/echo-kernel.ts');

	test('depth maps feedback 0.30..0.90 and wet 0..1 on one 10 ms ramp, out of range throws', () => {
		// [if] setDepth(x) does not ramp feedback to 0.3 + 0.6x, wet to x and dry to 1 - x by now + 10 ms, or accepts x outside [0, 1] [then] the law is broken, [else stop].
		const k = m.buildSpiralKernel(simContext(), { ...LOOP, depth: 0 });
		for (const key of ['input', 'output', 'setTime', 'gateInput', 'dispose', 'setDepth'])
			assert.ok(key in k, key);
		for (const [depth, fb] of [
			[0, 0.3],
			[0.5, 0.6],
			[1, 0.9]
		]) {
			k.setDepth(depth, 2);
			for (const [param, want] of [
				[k.feedback.gain, fb],
				[k.wet.gain, depth],
				[k.dry.gain, 1 - depth]
			]) {
				assert.ok(Math.abs(param.valueAt(2.01) - want) < 1e-12, `depth ${depth}`);
				assert.deepEqual(param.events.at(-1).t, 2 + m.SPIRAL_RAMP_SEC);
			}
		}
		for (const bad of [-0.01, 1.01, Number.NaN])
			assert.throws(() => k.setDepth(bad, 3), /spiral depth/);
	});

	test('stages come from the variant at build, nothing is created later, one stage is unity at 1 kHz', () => {
		// [if] a variant builds a stage count other than 2/4/6, a call creates a node, or one stage moves 1 kHz RMS over 0.1 dB [then] the diffuser is broken, [else stop].
		for (const variant of VARIANTS) {
			const ctx = simContext();
			const k = m.buildSpiralKernel(ctx, { ...LOOP, depth: 0.5, spiralVariant: variant });
			assert.equal(k.stages.length, m.SPIRAL_VARIANT_STAGES[variant]);
			const made = ctx.created;
			k.setTime(0.2, 0);
			k.setTime(0.3, 1);
			k.setDepth(0.9, 1);
			k.gateInput(false, 2);
			assert.equal(ctx.created, made, variant);
		}
		assert.equal(m.buildSpiralKernel(simContext(), { ...LOOP, depth: 0 }).stages.length, 4);
		const ctx = simContext();
		const stage = m.buildSpiralAllpassStage(ctx, 0.013, m.SPIRAL_ALLPASS_GAIN);
		const sink = ctx.createGain();
		stage.connectTo(sink);
		const y = render(ctx, stage.input, sink, 1, (i) => Math.sin((2 * Math.PI * 1000 * i) / SR));
		const rms = Math.sqrt(y.subarray(SR / 2).reduce((acc, v) => acc + v * v, 0) / (SR / 2));
		assert.ok(Math.abs(20 * Math.log10(rms / Math.SQRT1_2)) < 0.1, `stage RMS ${rms}`);
	});

	test('setTime glides delayTime on SPIRAL_GLIDE_SEC (overridable), refuses under 10 ms and over 4000 ms', () => {
		// [if] a warm setTime is not setTargetAtTime with the glide constant, or 9 ms / 4001 ms is accepted [then] the retune law is broken, [else stop].
		assert.equal(m.SPIRAL_GLIDE_SEC, 0.12);
		for (const [glideSec, want] of [
			[undefined, 0.12],
			[0.05, 0.05]
		]) {
			const k = m.buildSpiralKernel(simContext(), { ...LOOP, depth: 0.5, glideSec });
			k.setTime(0.5, 0);
			k.setTime(0.25, 1);
			assert.deepEqual(
				k.line.delayTime.events.map((e) => [e.kind, e.v, e.tau]),
				[
					['set', 0.5, undefined],
					['target', 0.25, want]
				]
			);
			assert.throws(() => k.setTime(0.009, 2), /spiral time 9 ms is outside \[10, 4000\] ms/);
			assert.throws(() => k.setTime(4.001, 2), /spiral time 4001 ms/);
		}
	});

	test('render: a retune bends the pitch during the glide and settles back', () => {
		// [if] a 1 kHz sine through depth 1 does not read above 1.5 kHz 60 to 100 ms into a 500 to 250 ms retune, or off 1 kHz once settled [then] no bend, [else stop].
		const ctx = simContext();
		const k = m.buildSpiralKernel(ctx, { ...LOOP, depth: 1, spiralVariant: 'light' });
		k.setTime(0.5, 0);
		const y = render(
			ctx,
			k.input,
			k.output,
			4,
			(i) => 0.25 * Math.sin((2 * Math.PI * 1000 * i) / SR),
			(i) => {
				if (i === 2 * SR) k.setTime(0.25, 2);
			}
		);
		const before = frequency(y, 1.8, 2);
		const during = frequency(y, 2.06, 2.1);
		const after = frequency(y, 3.8, 4);
		console.log(
			`spiral bend: ${before.toFixed(0)} Hz -> ${during.toFixed(0)} Hz -> ${after.toFixed(0)} Hz`
		);
		assert.ok(Math.abs(before - 1000) < 20 && Math.abs(after - 1000) < 20, `${before} ${after}`);
		assert.ok(during > 1500, `glide reads ${during} Hz`);
	});

	test('render: impulse at 1/2 beat, depth 0.7: onset within 2 ms, 3x echo density, tail under -90 dB by 12 s', () => {
		// [if] the first repeat lands over 2 ms off 1/2 beat, density is under 3x echo mode at feedback 0.72, or the tail is over -90 dBFS at 12 s [then] broken, [else stop].
		const ectx = simContext();
		const e = echo.buildEchoKernel(ectx, {
			...LOOP,
			mode: 'echo',
			maxDelaySec: 4,
			feedback: m.spiralFeedbackForDepth(0.7)
		});
		e.setTime(BEAT_SEC / 2, 0);
		const echoY = render(ectx, e.input, e.output, 2, impulse(0.5)).map((v) => 0.7 * v);
		const y = spiralImpulse(m, m.SPIRAL_DEFAULT_VARIANT, 12);
		const onset = y.findIndex((v, i) => i > 0 && Math.abs(v) > DB(-60)) / SR;
		console.log(
			`spiral medium: onset ${(onset * 1000).toFixed(2)} ms, density ${density(y).toFixed(3)} vs echo ${density(echoY).toFixed(3)}`
		);
		assert.ok(Math.abs(onset - BEAT_SEC / 2) <= 0.002, `onset ${onset}`);
		assert.ok(density(y) >= 3 * density(echoY), `${density(y)} vs echo ${density(echoY)}`);
		assert.ok(peak(y, 11, 12) < DB(-90), `tail ${peak(y, 11, 12)}`);
	});

	test('render: depth 1 with the soft clip in the loop stays under 0 dBFS, finite, no subnormal, dry latency 0', () => {
		// [if] full-scale bursts at depth 1 push a sample over 0 dBFS, NaN, infinity or subnormal, or depth 0 delays the dry by a sample [then] broken, [else stop].
		const ctx = simContext();
		const k = m.buildSpiralKernel(ctx, { ...LOOP, depth: 1, spiralVariant: 'dense' });
		k.setTime(m.SPIRAL_MIN_TIME_SEC, 0);
		const burst = (i) => (i % 4800 < 2400 ? Math.sign(Math.sin((2 * Math.PI * 220 * i) / SR)) : 0);
		const y = render(ctx, k.input, k.output, 3, burst, (i) => i === SR && k.setTime(1, 1));
		vetoes(y, 'depth 1');
		assert.ok(peak(y, 2.5, 3) > 0.01, 'loop went silent');
		const dctx = simContext();
		const d = m.buildSpiralKernel(dctx, { ...LOOP, depth: 0 });
		d.setTime(0.1, 0);
		const dry = render(dctx, d.input, d.output, 0.01, impulse(1));
		assert.equal(dry[0], 1);
	});

	test('the three variants render distinct density numbers and pass the same vetoes', () => {
		// [if] light, medium and dense print the same density, or any of them fails the onset, finite, 0 dBFS or tail vetoes [then] the variants are broken, [else stop].
		const counts = VARIANTS.map((variant) => {
			const y = spiralImpulse(m, variant, 12);
			vetoes(y, variant);
			const onset = y.findIndex((v, i) => i > 0 && Math.abs(v) > DB(-60)) / SR;
			assert.ok(Math.abs(onset - BEAT_SEC / 2) <= 0.002, `${variant} onset ${onset}`);
			assert.ok(peak(y, 11, 12) < DB(-90), `${variant} tail`);
			console.log(`spiral ${variant}: density ${density(y).toFixed(3)}`);
			return density(y);
		});
		assert.equal(new Set(counts).size, 3, counts.join(' '));
	});

	test('gateInput closes the send alone and dispose disconnects every node', () => {
		// [if] gateInput(false) ramps anything but the gate, or dispose leaves a node connected or allows a later call [then] trails or teardown break, [else stop].
		const ctx = simContext();
		const k = m.buildSpiralKernel(ctx, { ...LOOP, depth: 0.5 });
		k.gateInput(false, 1);
		assert.equal(k.gate.gain.valueAt(1.01), 0);
		assert.equal(k.feedback.gain.events.length, 0);
		k.dispose();
		assert.ok(k.nodes.every((n) => n.disconnected));
		assert.throws(() => k.setDepth(0.2, 2), /disposed/);
	});
});
