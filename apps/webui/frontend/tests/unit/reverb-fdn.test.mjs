// requirement: FXVERB-14205
// [if] reverb-fdn.ts loads in node [then] it yields mutually prime rate-scaled lengths, RT60 gains and named validation errors, [else stop].
// [if] the worklet runs [then] its params match REVERB_FDN_PARAMS, its coefficients match the TS law and process() allocates nothing, [else stop].
// [if] an impulse is rendered [then] T30 is within 10% of rt60 at 1.2, 2.6 and 6 s and RT60(4k)/RT60(500) is 0.4..0.6, [else stop].
// [if] 60 s of input then 60 s of silence is rendered [then] no sample is over 0 dBFS, NaN, infinite or subnormal, [else stop].
// [if] the input closes [then] the tail is under -90 dB in 1.5 RT60 + predelay + 0.1 s, and a 30 s freeze holds within 1 dB, [else stop].
// [if] predelay moves [then] the first wet sample moves by the same samples, and freeze and wet moves click at most 1.5, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, test } from 'node:test';

import * as fdn from '../../src/lib/rb/reverb-fdn.ts';

const PROCESSOR_PATH = new URL('../../src/lib/rb/reverb-fdn-processor.js', import.meta.url);
const SR = 48000;
const BLOCK = 128;
const FLT_MIN = 1.1754943508222875e-38;
const PARAM_NAMES = ['rt60', 'size', 'predelay', 'damping', 'wet', 'freeze'];

let Processor;

//--------------------------------------------------------------------- worklet host

before(async () => {
	globalThis.AudioWorkletProcessor = class {};
	globalThis.registerProcessor = (name, cls) =>
		name === fdn.REVERB_FDN_PROCESSOR_NAME && (Processor = cls);
	await import('../../src/lib/rb/reverb-fdn-processor.js');
	assert.ok(Processor, 'processor registered');
});

/** The processor reads the worklet global `sampleRate` at construction and on each coefficient update. */
function newProcessor(sampleRate = SR) {
	globalThis.sampleRate = sampleRate;
	return new Processor({ processorOptions: fdn.reverbFdnProcessorOptions(sampleRate) });
}

function defaults(overrides = {}) {
	const p = {};
	for (const name of PARAM_NAMES) p[name] = fdn.REVERB_FDN_PARAMS[name].default;
	return { ...p, ...overrides };
}

/** Render stereo out in 128-frame quanta; `paramsAt(frame)` gives the k-rate params of each quantum. */
function render(proc, frames, sourceAt, paramsAt) {
	const out = [new Float32Array(frames), new Float32Array(frames)];
	const inL = new Float32Array(BLOCK);
	const inR = new Float32Array(BLOCK);
	const params = {};
	for (const name of PARAM_NAMES) params[name] = new Float32Array(1);
	for (let start = 0; start < frames; start += BLOCK) {
		const n = Math.min(BLOCK, frames - start);
		for (let i = 0; i < n; i++) inL[i] = inR[i] = sourceAt(start + i);
		const p = paramsAt(start);
		for (const name of PARAM_NAMES) params[name][0] = p[name];
		const outputs = [[out[0].subarray(start, start + n), out[1].subarray(start, start + n)]];
		assert.equal(proc.process([[inL.subarray(0, n), inR.subarray(0, n)]], outputs, params), true);
	}
	return out;
}

const impulse = (i) => (i === 0 ? 1 : 0);

function noise(seed = 12345) {
	let s = seed;
	return () => {
		s ^= s << 13;
		s ^= s >>> 17;
		s ^= s << 5;
		return ((s >>> 0) / 4294967296) * 2 - 1;
	};
}

//--------------------------------------------------------------------- measures

/** RT60 from the Schroeder backward integral, a least-squares line over -5..-35 dB. */
function t30(h, sr) {
	const energy = new Float64Array(h.length + 1);
	for (let i = h.length - 1; i >= 0; i--) energy[i] = energy[i + 1] + h[i] * h[i];
	let n = 0;
	let sx = 0;
	let sy = 0;
	let sxx = 0;
	let sxy = 0;
	for (let i = 0; i < h.length; i += 16) {
		const db = 10 * Math.log10(energy[i] / energy[0]);
		if (db > -5 || db < -35) continue;
		const t = i / sr;
		n++;
		sx += t;
		sy += db;
		sxx += t * t;
		sxy += t * db;
	}
	return -60 / ((n * sxy - sx * sy) / (n * sxx - sx * sx));
}

/** RBJ constant-peak bandpass, one third of an octave wide (Q 4.32), applied offline. */
function bandpass(x, f0, sr) {
	const w = (2 * Math.PI * f0) / sr;
	const alpha = Math.sin(w) / (2 * 4.32);
	const a0 = 1 + alpha;
	const [b0, b2, a1, a2] = [alpha / a0, -alpha / a0, (-2 * Math.cos(w)) / a0, (1 - alpha) / a0];
	const y = new Float64Array(x.length);
	let [x1, x2, y1, y2] = [0, 0, 0, 0];
	for (let i = 0; i < x.length; i++) {
		y[i] = b0 * x[i] + b2 * x2 - a1 * y1 - a2 * y2;
		[x2, x1, y2, y1] = [x1, x[i], y1, y[i]];
	}
	return y;
}

function rms(x, from, to) {
	let s = 0;
	for (let i = from; i < to; i++) s += x[i] * x[i];
	return Math.sqrt(s / (to - from));
}

/** Max |y[n]-y[n-1]| near the event over the max in steady windows either side (as pitch-fx). */
function clickRatio(y, at, sr) {
	const slope = (from, to) => {
		let m = 0;
		for (let n = Math.max(1, from); n <= Math.min(y.length - 1, to); n++) {
			m = Math.max(m, Math.abs(y[n] - y[n - 1]));
		}
		return m;
	};
	const steady = Math.max(
		slope(at - Math.floor(0.3 * sr), at - Math.floor(0.1 * sr)),
		slope(at + Math.floor(0.25 * sr), at + Math.floor(0.45 * sr))
	);
	return slope(at - 128, at + 4096) / Math.max(steady, 1e-12);
}

//--------------------------------------------------------------------- tests

describe('pure law', () => {
	test('lengths are mutually prime and rate-scaled, gains hit -60 dB at rt60, validation names the param', () => {
		const gcd = (x, y) => (y === 0 ? x : gcd(y, x % y));
		for (const sr of [44100, 48000, 96000]) {
			for (const size of fdn.reverbFdnSizeGrid()) {
				const lengths = fdn.reverbFdnDelayLengths(sr, size);
				assert.equal(lengths.length, 8);
				for (let i = 0; i < 8; i++) {
					for (let j = i + 1; j < 8; j++) assert.equal(gcd(lengths[i], lengths[j]), 1);
				}
			}
		}
		const ref = fdn.reverbFdnDelayLengths(48000, 1);
		const scaled = fdn.reverbFdnDelayLengths(96000, 1);
		ref.forEach((l, i) => assert.ok(Math.abs(scaled[i] / (2 * l) - 1) < 0.025, `line ${i}`));
		const gains = fdn.reverbFdnDecayGains(ref, 2.6, SR);
		ref.forEach((l, i) => {
			const passes = (2.6 * SR) / l;
			assert.ok(Math.abs(20 * Math.log10(gains[i]) * passes + 60) < 1e-9);
		});
		assert.doesNotThrow(() => fdn.validateReverbFdnParams(defaults()));
		const bad = { rt60: 0.2, size: 2, predelay: NaN, damping: 0.4, wet: -1, freeze: 0.5 };
		for (const [name, value] of Object.entries(bad)) {
			const params = defaults({ [name]: value });
			assert.throws(() => fdn.validateReverbFdnParams(params), new RegExp(`reverb ${name}`));
		}
		assert.throws(() => fdn.reverbFdnDelayLengths(SR, 1.6), /reverb size/);
	});
});

describe('processor', () => {
	test('params match the TS table, coefficients mirror the TS law at every corner, process() allocates nothing', () => {
		const proc = newProcessor();
		const descriptors = proc.constructor.parameterDescriptors;
		assert.deepEqual([...descriptors.map((d) => d.name)], PARAM_NAMES);
		for (const d of descriptors) {
			const want = fdn.REVERB_FDN_PARAMS[d.name];
			const got = [d.minValue, d.maxValue, d.defaultValue, d.automationRate];
			assert.deepEqual(got, [want.min, want.max, want.default, 'k-rate']);
		}
		const corners = [0.3, 2.6, 10].flatMap((rt60) =>
			[0.5, 1].flatMap((damping) => [0.5, 1.5].map((size) => ({ rt60, damping, size })))
		);
		for (const sr of [44100, 48000, 96000]) {
			const p = newProcessor(sr);
			for (const { rt60, damping, size } of corners) {
				render(
					p,
					BLOCK,
					() => 0,
					() => defaults({ rt60, damping, size })
				);
				const lengths = fdn.reverbFdnDelayLengths(sr, size);
				const [r, d] = [Math.fround(rt60), Math.fround(damping)];
				const want = fdn.reverbFdnLoopCoefficients(lengths, r, d, sr);
				assert.deepEqual([[...p.b], [...p.a], p.outputGain], [want.b, want.a, want.outputGain]);
				want.b.forEach((b, k) => assert.ok(want.a[k] >= 0 && b / (1 - want.a[k]) < 1, `stable`));
			}
		}
		const src = readFileSync(PROCESSOR_PATH, 'utf8');
		const start = src.indexOf('process(inputs, outputs, parameters) {');
		const body = src.slice(start, src.indexOf('\nregisterProcessor')).replace(/\/\/.*$/gm, '');
		const banned = /new |=>|function|= \[|= \{|\.\.\.|\.slice\(|\.map\(|\.from\(/;
		assert.equal(body.match(banned), null, 'process() allocates');
	});

	test('T30 is within 10% of rt60 at 1.2, 2.6 and 6 s; RT60(4 kHz)/RT60(500 Hz) is 0.4..0.6 at default damping', () => {
		for (const rt60 of [1.2, 2.6, 6]) {
			const [h] = render(newProcessor(), Math.ceil(1.5 * rt60 * SR), impulse, () =>
				defaults({ rt60, damping: 1, predelay: 0 })
			);
			const measured = t30(h, SR);
			assert.ok(Math.abs(measured / rt60 - 1) < 0.1, `rt60 ${rt60}: measured ${measured}`);
		}
		const [h] = render(newProcessor(), 4 * SR, impulse, () => defaults({ predelay: 0 }));
		const low = t30(bandpass(h, 500, SR), SR);
		const high = t30(bandpass(h, 4000, SR), SR);
		assert.ok(Math.abs(low / 2.6 - 1) < 0.1, `500 Hz rt60 ${low}`);
		assert.ok(high / low >= 0.4 && high / low <= 0.6, `ratio ${high / low}`);
	});

	test('60 s of -1 dBFS input then 60 s of silence: under 0 dBFS, no NaN, infinity or subnormal in output or state', () => {
		const peak = 10 ** (-1 / 20);
		const sources = { noise: noise(), sine: (i) => peak * Math.sin((2 * Math.PI * 220 * i) / SR) };
		for (const [kind, gen] of Object.entries(sources)) {
			for (const p of [
				defaults({ rt60: 10, size: 1.5, damping: 1 }),
				defaults({ rt60: 10, size: 0.5, damping: 1 }),
				defaults({ rt60: 0.3, size: 0.5 })
			]) {
				const proc = newProcessor();
				const src = (i) => (i < 60 * SR ? (kind === 'noise' ? peak * gen() : gen(i)) : 0);
				const out = render(proc, 120 * SR, src, () => p);
				const top = Math.max(...out.map((ch) => ch.reduce((m, s) => Math.max(m, Math.abs(s)), 0)));
				assert.ok(top < 1, `${kind} rt60 ${p.rt60} size ${p.size}: peak ${top}`);
				for (const x of [...out, proc.lines, proc.lp, proc.preL, proc.preR]) {
					const bad = x.find((s) => !Number.isFinite(s) || (s !== 0 && Math.abs(s) < FLT_MIN));
					assert.equal(bad, undefined, `${kind} rt60 ${p.rt60}: non-finite or subnormal ${bad}`);
				}
			}
		}
	});

	test('closed input: under -90 dB re steady state by 1.5 RT60 + predelay + 0.1 s; a 30 s freeze holds within 1 dB', () => {
		const gen = noise();
		for (const rt60 of [1.2, 2.6]) {
			const p = defaults({ rt60 });
			const cut = 2 * SR;
			const deadline = cut + Math.round((1.5 * rt60 + p.predelay + 0.1) * SR);
			const [y] = render(
				newProcessor(),
				deadline + SR / 10,
				(i) => (i < cut ? gen() : 0),
				() => p
			);
			const steady = rms(y, cut - SR / 2, cut);
			assert.ok(
				20 * Math.log10(rms(y, deadline, deadline + SR / 10) / steady) < -90,
				`rt60 ${rt60}`
			);
		}
		// Input closed: after the gate ramp, silence and loud noise in leave the frozen tail identical.
		const freezeAt = 2 * SR;
		const shut = freezeAt + Math.round(fdn.REVERB_FDN_RAMP_SEC * SR);
		const frozen = (after) => {
			const pre = noise(9);
			const src = (i) => (i < shut ? pre() : after());
			return render(newProcessor(), 33 * SR, src, (f) =>
				defaults({ freeze: f >= freezeAt ? 1 : 0 })
			)[0];
		};
		const y = frozen(() => 0);
		const loud = noise(4);
		assert.deepEqual(frozen(loud), y);
		const first = rms(y, freezeAt + SR / 10, freezeAt + SR / 10 + SR);
		for (let s = 3; s < 33; s++) {
			assert.ok(Math.abs(20 * Math.log10(rms(y, s * SR, (s + 1) * SR) / first)) < 1, `second ${s}`);
		}
	});

	test('predelay shifts the first wet sample by its samples; freeze on, freeze off and a wet move click at most 1.5', () => {
		const onset = (predelay) => {
			const [y] = render(newProcessor(), SR / 4, impulse, () => defaults({ predelay }));
			return y.findIndex((s) => s !== 0);
		};
		const base = onset(0);
		assert.equal(base, fdn.reverbFdnDelayLengths(SR, 1)[0]);
		for (const predelay of [0.02, 0.05, 0.1])
			assert.equal(onset(predelay) - base, Math.round(predelay * SR));
		const gen = noise(777);
		const [on, off, wetMove] = [3 * SR, 4 * SR, 5 * SR];
		const [y] = render(
			newProcessor(),
			6 * SR,
			() => 0.5 * gen(),
			(f) => defaults({ freeze: f >= on && f < off ? 1 : 0, wet: f >= wetMove ? 0.3 : 1 })
		);
		for (const at of [on, off, wetMove])
			assert.ok(clickRatio(y, at, SR) <= 1.5, `click at ${at / SR} s`);
	});
});
