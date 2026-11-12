// requirement: FXPITCH-10222
// [if] pitch percent escapes -50..100 or the seven V10 step labels [then] broken
// [if] wet pitch at +100% / -50% / 0% misses the 440 Hz fixture tolerances [then] broken
// [if] depth 0 or kernel off is not bit-exact dry [then] broken
// [if] variant_fx_pitch_window is missing 20/40/80 or live flip fails [then] broken
// [if] latency exceeds W + 128 or disagrees with latencySamples [then] broken
// [if] any sample clips, is non-finite or subnormal on the safety grid [then] broken
// [if] engage/release/step/variant click ratio exceeds 1.5 [then] broken
// [if] the processor allocates per quantum [then] broken
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { before, describe, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const PROCESSOR_PATH = 'src/lib/rb/pitch-shift-processor.js';
const KERNEL_PATH = 'src/lib/rb/pitch-fx-kernel.ts';
const RENDER_QUANTUM = 128;
const SUBNORMAL_EPS = 1.1754943508222875e-38;
const PITCH_WINDOWS_MS = [20, 40, 80];

let mod;
let variantPrefs;
let variantSetting;
let ProcessorClass;

//-----------------------------------------------------------------------------
// worklet host
//-----------------------------------------------------------------------------

function installProcessor() {
	Object.defineProperty(globalThis, 'performance', {
		configurable: true,
		writable: true,
		value: { now: () => 0 }
	});
	globalThis.AudioWorkletProcessor = class {
		constructor() {
			this.port = {
				onmessage: null,
				postMessage(message) {
					this.__posts.push(message);
					if (this.onmessage) {
						this.onmessage({ data: message });
					}
				},
				__posts: []
			};
		}
	};
	let registered = null;
	globalThis.registerProcessor = (name, ctor) => {
		registered = { name, ctor };
	};
	globalThis.currentTime = 0;
	globalThis.sampleRate = 48000;
	// eslint-disable-next-line no-new-func
	new Function(readFrontendSource(PROCESSOR_PATH))();
	assert.equal(registered?.name, 'mdt-pitch-shift');
	return registered.ctor;
}

function windowSamples(ms, sampleRate) {
	return 2 * Math.round((ms * sampleRate) / 2000);
}

function makeProcessor({ sampleRate, windowMs, maxWindowMs = 80 }) {
	globalThis.sampleRate = sampleRate;
	return new ProcessorClass({
		processorOptions: {
			windowMs,
			maxWindowMs,
			rampSec: mod.PITCH_FX_RAMP_SEC,
			crossfadeSec: mod.PITCH_FX_WINDOW_XFADE_SEC
		}
	});
}

/**
 * Render through the real processor. Events apply at the quantum containing
 * atFrame: depth, percent, active, window (ms port message).
 */
function render({ sampleRate, windowMs, input, quanta, events = [] }) {
	const proc = makeProcessor({ sampleRate, windowMs });
	const totalFrames = quanta * RENDER_QUANTUM;
	let ch0 = input[0];
	let ch1 = input[1] ?? input[0];
	if (ch0.length < totalFrames) {
		const p0 = new Float32Array(totalFrames);
		const p1 = new Float32Array(totalFrames);
		p0.set(ch0);
		p1.set(ch1.subarray(0, Math.min(ch1.length, totalFrames)));
		ch0 = p0;
		ch1 = p1;
	}

	const depth = new Float32Array([0]);
	const percent = new Float32Array([-50]);
	const active = new Float32Array([0]);
	const parameters = { depth, percent, active };

	const out0 = new Float32Array(totalFrames);
	const out1 = new Float32Array(totalFrames);
	const outputs = [new Float32Array(RENDER_QUANTUM), new Float32Array(RENDER_QUANTUM)];
	let eventIdx = 0;

	for (let q = 0; q < quanta; q += 1) {
		const base = q * RENDER_QUANTUM;
		while (eventIdx < events.length && events[eventIdx].atFrame <= base + RENDER_QUANTUM - 1) {
			const ev = events[eventIdx];
			if (ev.depth !== undefined) depth[0] = ev.depth;
			if (ev.percent !== undefined) percent[0] = ev.percent;
			if (ev.active !== undefined) active[0] = ev.active;
			if (ev.window !== undefined) {
				proc.port.postMessage({ kind: 'window', windowMs: ev.window });
			}
			eventIdx += 1;
		}
		const inputs = [
			[ch0.subarray(base, base + RENDER_QUANTUM), ch1.subarray(base, base + RENDER_QUANTUM)]
		];
		outputs[0] = out0.subarray(base, base + RENDER_QUANTUM);
		outputs[1] = out1.subarray(base, base + RENDER_QUANTUM);
		proc.process(inputs, [outputs], parameters);
	}
	return [out0, out1];
}

function sineFixture({ sampleRate, freq, amp, frames }) {
	const ch = new Float32Array(frames);
	for (let i = 0; i < frames; i += 1) {
		ch[i] = amp * Math.sin((2 * Math.PI * freq * i) / sampleRate);
	}
	return [ch];
}

function noiseFixture(frames, seed = 1) {
	let s = seed;
	const ch = new Float32Array(frames);
	for (let i = 0; i < frames; i += 1) {
		s = (s * 48271) % 2147483647;
		ch[i] = (s / 2147483647) * 2 - 1;
	}
	return [ch];
}

function bytesEqual(a, b) {
	return (
		a.length === b.length &&
		Buffer.from(a.buffer, a.byteOffset, a.byteLength).equals(
			Buffer.from(b.buffer, b.byteOffset, b.byteLength)
		)
	);
}

//-----------------------------------------------------------------------------
// pitch measure (Hann + radix-2 FFT peak)
//-----------------------------------------------------------------------------

function fftInPlace(re, im) {
	const n = re.length;
	for (let i = 1, j = 0; i < n; i += 1) {
		let bit = n >> 1;
		for (; j & bit; bit >>= 1) {
			j ^= bit;
		}
		j ^= bit;
		if (i < j) {
			const tr = re[i];
			re[i] = re[j];
			re[j] = tr;
			const ti = im[i];
			im[i] = im[j];
			im[j] = ti;
		}
	}
	for (let len = 2; len <= n; len <<= 1) {
		const ang = (-2 * Math.PI) / len;
		const wlenRe = Math.cos(ang);
		const wlenIm = Math.sin(ang);
		for (let i = 0; i < n; i += len) {
			let wRe = 1;
			let wIm = 0;
			for (let j = 0; j < len / 2; j += 1) {
				const uRe = re[i + j];
				const uIm = im[i + j];
				const vRe = re[i + j + len / 2] * wRe - im[i + j + len / 2] * wIm;
				const vIm = re[i + j + len / 2] * wIm + im[i + j + len / 2] * wRe;
				re[i + j] = uRe + vRe;
				im[i + j] = uIm + vIm;
				re[i + j + len / 2] = uRe - vRe;
				im[i + j + len / 2] = uIm - vIm;
				const nwRe = wRe * wlenRe - wIm * wlenIm;
				wIm = wRe * wlenIm + wIm * wlenRe;
				wRe = nwRe;
			}
		}
	}
}

/** Fundamental from the largest FFT bin between 50 Hz and 2 kHz (power-of-2 length). */
function fftPeakHz(samples, sampleRate) {
	const n = samples.length;
	assert.equal(n & (n - 1), 0, 'FFT length must be a power of 2');
	const re = new Float64Array(n);
	const im = new Float64Array(n);
	for (let i = 0; i < n; i += 1) {
		const w = 0.5 * (1 - Math.cos((2 * Math.PI * i) / (n - 1)));
		re[i] = samples[i] * w;
	}
	fftInPlace(re, im);
	const binHz = sampleRate / n;
	const minBin = Math.max(1, Math.ceil(50 / binHz));
	const maxBin = Math.min(n >> 1, Math.floor(2000 / binHz));
	const logMag = (k) => {
		const m = re[k] * re[k] + im[k] * im[k];
		return Math.log(Math.max(m, 1e-40));
	};
	let bestBin = minBin;
	let bestLog = -Infinity;
	for (let k = minBin; k <= maxBin; k += 1) {
		const lm = logMag(k);
		if (lm > bestLog) {
			bestLog = lm;
			bestBin = k;
		}
	}
	const km = bestBin > minBin ? bestBin - 1 : bestBin;
	const kp = bestBin < maxBin ? bestBin + 1 : bestBin;
	const lmM = logMag(km);
	const lm0 = logMag(bestBin);
	const lmP = logMag(kp);
	const denom = lmM - 2 * lm0 + lmP;
	let refined = bestBin;
	if (Math.abs(denom) > 1e-12) {
		refined = bestBin + (0.5 * (lmM - lmP)) / denom;
	}
	return refined * binHz;
}

//-----------------------------------------------------------------------------
// clickRatio: max |y[n]-y[n-1]| over [event-128, event+4096] divided by the
// max over steady windows [event-0.3s, event-0.1s] and [event+0.25s, event+0.45s].
//-----------------------------------------------------------------------------

function maxSlope(y, from, to) {
	let m = 0;
	for (let n = Math.max(1, from); n <= to; n += 1) {
		const d = Math.abs(y[n] - y[n - 1]);
		if (d > m) m = d;
	}
	return m;
}

function clickRatio(y, eventFrame, sr) {
	const post = maxSlope(y, eventFrame - 128, Math.min(y.length - 1, eventFrame + 4096));
	const pre = maxSlope(
		y,
		Math.max(1, eventFrame - Math.floor(0.3 * sr)),
		Math.max(1, eventFrame - Math.floor(0.1 * sr))
	);
	const postSteady = maxSlope(
		y,
		Math.max(1, eventFrame + Math.floor(0.25 * sr)),
		Math.min(y.length - 1, eventFrame + Math.floor(0.45 * sr))
	);
	const denom = Math.max(pre, postSteady, 1e-12);
	return post / denom;
}

//-----------------------------------------------------------------------------
// kernel bridge (variant / dispose)
//-----------------------------------------------------------------------------

function createKernelBridge({ sampleRate = 48000, variant = '40' } = {}) {
	const prefs = variantPrefs.makeVariantPrefAccessors({ variants: {} }, () => {});
	prefs.setVariant(mod.PITCH_FX_WINDOW_VARIANT.id, variant);
	let proc = null;
	let procOptions = null;
	const paramLog = [];

	const ctx = {
		sampleRate,
		currentTime: 0,
		createGain() {
			return { connect() {}, disconnect() {} };
		}
	};

	const createWorkletNode = (_c, _name, options) => {
		procOptions = options.processorOptions;
		proc = makeProcessor({
			sampleRate,
			windowMs: procOptions.windowMs,
			maxWindowMs: procOptions.maxWindowMs
		});
		return {
			parameters: {
				get(name) {
					const map = { depth: 'depth', percent: 'percent', active: 'active' };
					return {
						setValueAtTime(v, at) {
							paramLog.push({ name: map[name], value: v, at });
						}
					};
				}
			},
			port: proc.port,
			connect() {},
			disconnect() {}
		};
	};

	const kernel = mod.createPitchFxKernel(ctx, {
		readVariant: prefs.readVariant,
		createWorkletNode
	});

	function bridgeRender({ input, quanta, startEvents = [], frameEvents = [] }) {
		const depth = new Float32Array([0]);
		const percent = new Float32Array([-50]);
		const active = new Float32Array([0]);
		const parameters = { depth, percent, active };
		const ch0 = input[0];
		const ch1 = input[1] ?? input[0];
		const totalFrames = quanta * RENDER_QUANTUM;
		const out0 = new Float32Array(totalFrames);
		const out1 = new Float32Array(totalFrames);
		const outputs = [new Float32Array(RENDER_QUANTUM), new Float32Array(RENDER_QUANTUM)];

		let ev = 0;
		for (const se of startEvents) {
			if (se.depth !== undefined) depth[0] = se.depth;
			if (se.percent !== undefined) percent[0] = se.percent;
			if (se.active !== undefined) active[0] = se.active;
		}

		let frameIdx = 0;
		for (let q = 0; q < quanta; q += 1) {
			const base = q * RENDER_QUANTUM;
			const t = base / sampleRate;
			while (
				frameIdx < frameEvents.length &&
				frameEvents[frameIdx].atFrame <= base + RENDER_QUANTUM - 1
			) {
				const fe = frameEvents[frameIdx];
				if (fe.variant !== undefined) {
					prefs.setVariant(mod.PITCH_FX_WINDOW_VARIANT.id, fe.variant);
				}
				frameIdx += 1;
			}
			while (ev < paramLog.length && paramLog[ev].at <= t + RENDER_QUANTUM / sampleRate) {
				const p = paramLog[ev];
				if (p.name) parameters[p.name][0] = p.value;
				ev += 1;
			}
			const inputs = [
				[ch0.subarray(base, base + RENDER_QUANTUM), ch1.subarray(base, base + RENDER_QUANTUM)]
			];
			outputs[0] = out0.subarray(base, base + RENDER_QUANTUM);
			outputs[1] = out1.subarray(base, base + RENDER_QUANTUM);
			proc.process(inputs, [outputs], parameters);
		}
		return [out0, out1];
	}

	return { kernel, prefs, proc, procOptions, bridgeRender };
}

before(async () => {
	const entry = await loadTypeScriptModule('tests/unit/pitch-fx-kernel-entry.mjs');
	mod = entry.kernel;
	variantPrefs = entry.variantPrefs;
	variantSetting = entry.variantSetting;
	ProcessorClass = installProcessor();
});

describe('parsePitchPercent range', () => {
	test('accepts numeric and step labels', () => {
		assert.equal(mod.parsePitchPercent(-50), -50);
		assert.equal(mod.parsePitchPercent(0), 0);
		assert.equal(mod.parsePitchPercent(100), 100);
		assert.equal(mod.parsePitchPercent(12.5), 12.5);
		assert.equal(mod.parsePitchPercent('26%'), 26);
		assert.equal(mod.pitchRatio(-50), 0.5);
		assert.equal(mod.pitchRatio(0), 1);
		assert.equal(mod.pitchRatio(100), 2);
		assert.equal(mod.pitchRatio('26%'), 1.26);
		assert.equal(mod.pitchRatio(12.5), 1.125);
	});

	test('refuses out-of-range values', () => {
		for (const bad of [NaN, Infinity, -50.01, 100.01, '12%', '+26%', ' 26%', null, {}, 101]) {
			assert.throws(() => mod.parsePitchPercent(bad), RangeError);
		}
	});

	test('kernel setters refuse bad depth and percent', () => {
		const ctx = {
			sampleRate: 48000,
			currentTime: 0,
			createGain: () => ({ connect() {}, disconnect() {} })
		};
		const k = mod.createPitchFxKernel(ctx, {
			readVariant: () => '40',
			createWorkletNode: () => ({
				parameters: {
					get: () => ({ setValueAtTime() {} })
				},
				port: { postMessage() {}, onmessage: null },
				connect() {},
				disconnect() {}
			})
		});
		assert.throws(() => k.setPercent(200, 0), RangeError);
		assert.throws(() => k.setDepth(-0.01, 0), RangeError);
		assert.throws(() => k.setDepth(1.01, 0), RangeError);
		assert.throws(() => k.setDepth(NaN, 0), RangeError);
		k.dispose();
	});
});

describe('pitch frequency', () => {
	const FFT_LEN = 65536;
	const cases = [
		[0, 440, 0.001],
		[-50, 220, 0.01],
		[100, 880, 0.01]
	];
	for (const windowMs of PITCH_WINDOWS_MS) {
		for (const [percent, expectedHz, relTol] of cases) {
			test(`window ${windowMs} ms at ${percent}% peaks near ${expectedHz} Hz`, () => {
				const sr = 48000;
				const skip = Math.floor(0.25 * sr);
				const frames = skip + FFT_LEN + sr;
				const input = sineFixture({ sampleRate: sr, freq: 440, amp: 0.5, frames });
				const [out] = render({
					sampleRate: sr,
					windowMs,
					input,
					quanta: Math.ceil(frames / RENDER_QUANTUM),
					events: [{ atFrame: 0, depth: 1, active: 1, percent }]
				});
				const slice = out.subarray(skip, skip + FFT_LEN);
				const hz = fftPeakHz(slice, sr);
				const err = Math.abs(hz - expectedHz) / expectedHz;
				assert.ok(
					err <= relTol,
					`${percent}% window ${windowMs} ms: peak ${hz.toFixed(2)} Hz, expected ${expectedHz}, rel err ${err}`
				);
			});
		}
	}
});

describe('dry and wet endpoints', () => {
	test('depth 0 and kernel off are bit-exact dry', () => {
		const sr = 48000;
		const frames = sr;
		for (const input of [
			sineFixture({ sampleRate: sr, freq: 440, amp: 0.5, frames }),
			noiseFixture(frames)
		]) {
			const quanta = Math.ceil(frames / RENDER_QUANTUM);
			for (const events of [
				[{ atFrame: 0, depth: 0, active: 1, percent: 26 }],
				[{ atFrame: 0, depth: 1, active: 0, percent: 26 }]
			]) {
				const [out] = render({ sampleRate: sr, windowMs: 40, input, quanta, events });
				assert.ok(bytesEqual(input[0], out));
			}
		}
	});

	test('wet only at 0% is pure delay 1 + W/2', () => {
		const sr = 48000;
		const windowMs = 40;
		const W = windowSamples(windowMs, sr);
		const delay = 1 + W / 2;
		const frames = sr * 2;
		const input = noiseFixture(frames);
		const rampSamples = Math.ceil(mod.PITCH_FX_RAMP_SEC * sr) + 64;
		const skip = rampSamples + delay + W;
		const [out] = render({
			sampleRate: sr,
			windowMs,
			input,
			quanta: Math.ceil(frames / RENDER_QUANTUM),
			events: [{ atFrame: 0, depth: 1, active: 1, percent: 0 }]
		});
		const a = input[0].subarray(skip - delay, skip - delay + 4096);
		const b = out.subarray(skip, skip + 4096);
		assert.ok(bytesEqual(a, b));
	});

	test('engage then release is bit-exact dry after ramp', () => {
		const sr = 48000;
		const frames = sr * 2;
		const ramp = Math.ceil(mod.PITCH_FX_RAMP_SEC * sr) + 8;
		const mid = Math.floor(0.6 * sr);
		const settle = mid + ramp + Math.floor(0.05 * sr);
		const input = sineFixture({ sampleRate: sr, freq: 440, amp: 0.5, frames });
		const [out] = render({
			sampleRate: sr,
			windowMs: 40,
			input,
			quanta: Math.ceil(frames / RENDER_QUANTUM),
			events: [
				{ atFrame: 0, depth: 1, active: 1, percent: 26 },
				{ atFrame: mid, active: 0, depth: 1, percent: 26 }
			]
		});
		assert.ok(bytesEqual(input[0].subarray(settle), out.subarray(settle)));
	});
});

describe('variant_fx_pitch_window', () => {
	test('registry and live flip', () => {
		const def = variantSetting.variantSetting('variant_fx_pitch_window');
		assert.deepEqual(
			def.control.options.map((o) => o.value),
			['20', '40', '80']
		);
		assert.equal(def.defaultValue, '40');
		assert.equal(def.devOnly, true);

		const { kernel, prefs, procOptions, proc } = createKernelBridge({ variant: '40' });
		assert.equal(procOptions.windowMs, 40);
		assert.equal(kernel.windowMs, 40);

		const postsBefore = proc.port.__posts.length;
		prefs.setVariant('variant_fx_pitch_window', '80');
		assert.equal(proc.port.__posts.length - postsBefore, 1);
		assert.deepEqual(proc.port.__posts.at(-1), { kind: 'window', windowMs: 80 });
		assert.equal(kernel.windowMs, 80);

		kernel.dispose();
		const afterDispose = proc.port.__posts.length;
		prefs.setVariant('variant_fx_pitch_window', '20');
		assert.equal(proc.port.__posts.length, afterDispose);
	});
});

describe('latency', () => {
	for (const windowMs of PITCH_WINDOWS_MS) {
		for (const sr of [44100, 48000]) {
			test(`${windowMs} ms at ${sr} Hz`, () => {
				const W = windowSamples(windowMs, sr);
				const expectDelay = 1 + W / 2;
				const frames = sr * 2;
				const input = [new Float32Array(frames)];
				const impulseAt = 2048;
				input[0][impulseAt] = 1;
				const [out] = render({
					sampleRate: sr,
					windowMs,
					input,
					quanta: Math.ceil(frames / RENDER_QUANTUM),
					events: [{ atFrame: 0, depth: 1, active: 1, percent: 0 }]
				});
				let peak = impulseAt;
				let peakVal = 0;
				for (let i = impulseAt; i < impulseAt + W + 256; i += 1) {
					const v = Math.abs(out[i]);
					if (v > peakVal) {
						peakVal = v;
						peak = i;
					}
				}
				assert.equal(peak - impulseAt, expectDelay);
				assert.ok(expectDelay <= W + RENDER_QUANTUM);
				const { kernel } = createKernelBridge({ sampleRate: sr, variant: String(windowMs) });
				assert.equal(kernel.latencySamples, expectDelay);
				kernel.dispose();

				for (const pct of [-50, 100]) {
					const [o2] = render({
						sampleRate: sr,
						windowMs,
						input,
						quanta: Math.ceil(frames / RENDER_QUANTUM),
						events: [{ atFrame: 0, depth: 1, active: 1, percent: pct }]
					});
					let first = frames;
					for (let i = impulseAt; i < impulseAt + W + RENDER_QUANTUM; i += 1) {
						if (Math.abs(o2[i]) > 1e-4) {
							first = i;
							break;
						}
					}
					assert.ok(first <= impulseAt + W + RENDER_QUANTUM);
				}
			});
		}
	}
});

describe('safety grid', () => {
	const sr = 48000;
	const fixtureFrames = Math.floor(0.25 * sr);
	const quanta = Math.ceil(fixtureFrames / RENDER_QUANTUM);

	function fixtures() {
		return [
			[new Float32Array(fixtureFrames)],
			sineFixture({ sampleRate: sr, freq: 440, amp: 1, frames: fixtureFrames }),
			(() => {
				const ch = new Float32Array(fixtureFrames);
				for (let i = 0; i < fixtureFrames; i += 1) {
					ch[i] =
						0.5 * Math.sin((2 * Math.PI * 55 * i) / sr) +
						0.5 * Math.sin((2 * Math.PI * 3520 * i) / sr);
				}
				return [ch];
			})(),
			noiseFixture(fixtureFrames),
			(() => {
				const ch = new Float32Array(fixtureFrames);
				for (let i = 0; i < fixtureFrames; i += RENDER_QUANTUM * 37) ch[i] = 1;
				return [ch];
			})(),
			(() => {
				const ch = new Float32Array(fixtureFrames);
				for (let i = 0; i < fixtureFrames; i += 1) {
					const t = i / sr;
					ch[i] = Math.max(1e-20, Math.exp(-t * 8)) * Math.sin(2 * Math.PI * 440 * t);
					if (Math.abs(ch[i]) < 1e-20) ch[i] = 0;
				}
				return [ch];
			})()
		];
	}

	test('finite, bounded, no subnormals', () => {
		const percents = [-50, -37, -25, 0, 12.5, 26, 50, 100];
		const depths = [0, 0.25, 0.5, 0.75, 1];
		for (const windowMs of PITCH_WINDOWS_MS) {
			for (const input of fixtures()) {
				for (const percent of percents) {
					for (const depth of depths) {
						const [out] = render({
							sampleRate: sr,
							windowMs,
							input,
							quanta,
							events: [{ atFrame: 0, depth, active: 1, percent }]
						});
						for (let i = 0; i < out.length; i += 1) {
							const v = out[i];
							assert.ok(Number.isFinite(v));
							assert.ok(Math.abs(v) <= 1);
							assert.ok(!(v !== 0 && Math.abs(v) < SUBNORMAL_EPS));
						}
					}
				}
			}
		}
	});
});

describe('clicks', () => {
	for (const windowMs of PITCH_WINDOWS_MS) {
		test(`window ${windowMs} ms click ratio <= 1.5`, () => {
			const sr = 48000;
			const frames = sr * 2;
			const input = sineFixture({ sampleRate: sr, freq: 440, amp: 0.5, frames });
			const quanta = Math.ceil(frames / RENDER_QUANTUM);
			const mid = Math.floor(0.5 * sr);
			const reEngage = mid + Math.floor(0.25 * sr);
			const scenarios = [
				{
					at: mid,
					events: [
						{ atFrame: 0, active: 0, depth: 0, percent: 100 },
						{ atFrame: mid, active: 1, depth: 1, percent: 100 }
					]
				},
				{
					at: mid,
					events: [
						{ atFrame: 0, active: 0, depth: 0, percent: -50 },
						{ atFrame: mid, active: 1, depth: 1, percent: -50 }
					]
				},
				{
					at: mid,
					events: [
						{ atFrame: 0, active: 1, depth: 1, percent: -50 },
						{ atFrame: mid, active: 0, depth: 1, percent: -50 }
					]
				},
				{
					at: mid,
					events: [
						{ atFrame: 0, active: 1, depth: 1, percent: -50 },
						{ atFrame: mid, percent: 100 }
					]
				},
				{
					at: mid,
					events: [
						{ atFrame: 0, active: 1, depth: 0.5, percent: -50 },
						{ atFrame: mid, percent: 100 }
					]
				},
				{
					at: reEngage,
					events: [
						{ atFrame: 0, active: 1, depth: 1, percent: -50 },
						{ atFrame: mid, active: 0, depth: 1, percent: -50 },
						{ atFrame: reEngage, active: 1, depth: 1, percent: -50 }
					]
				}
			];
			for (const sc of scenarios) {
				const [out] = render({ sampleRate: sr, windowMs, input, quanta, events: sc.events });
				assert.ok(clickRatio(out, sc.at, sr) <= 1.5, `click at frame ${sc.at}`);
			}

			const bridge = createKernelBridge({ sampleRate: sr, variant: '40' });
			bridge.kernel.setDepth(1, 0);
			bridge.kernel.setPercent(26, 0);
			bridge.kernel.setActive(true, 0);
			const kInput = sineFixture({ sampleRate: sr, freq: 440, amp: 0.5, frames });
			const flipFrame = mid;
			const [bout] = bridge.bridgeRender({
				input: kInput,
				quanta,
				frameEvents: [{ atFrame: flipFrame, variant: '80' }]
			});
			assert.ok(clickRatio(bout, flipFrame, sr) <= 1.5);
			bridge.kernel.dispose();

			const bridge2 = createKernelBridge({ sampleRate: sr, variant: '80' });
			bridge2.kernel.setDepth(1, 0);
			bridge2.kernel.setPercent(26, 0);
			bridge2.kernel.setActive(true, 0);
			const [bout2] = bridge2.bridgeRender({
				input: kInput,
				quanta,
				frameEvents: [{ atFrame: flipFrame, variant: '20' }]
			});
			assert.ok(clickRatio(bout2, flipFrame, sr) <= 1.5);
			bridge2.kernel.dispose();
		});
	}
});

describe('no per-quantum allocation', () => {
	test('processor hot path is allocation-free', () => {
		const src = readFrontendSource(PROCESSOR_PATH);
		assert.ok(PROCESSOR_PATH.endsWith('-processor.js'));
		assert.ok(!/\bimport\b/.test(src));
		assert.ok(!/\bexport\b/.test(src));
		const kernelSrc = readFrontendSource(KERNEL_PATH);
		assert.ok(kernelSrc.includes('pitch-shift-processor.js?url'));

		const noComments = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
		const ctorEnd = noComments.indexOf('	static _windowSamples');
		assert.ok(ctorEnd > 0);
		const body = noComments.slice(ctorEnd);
		assert.ok(/process\(/.test(body));
		assert.ok(!/\bnew\s/.test(body));
		assert.ok(!/=>/.test(body));
		assert.ok(!/\[\s*\]/.test(body));
		assert.ok(!/\{\s*\}/.test(body));
		assert.ok(!/\.\.\./.test(body));
		assert.ok(!/\.(slice|map|filter|concat|subarray|from)\(/.test(body));
	});
});

describe('CPU diagnostic', () => {
	test('microseconds per quantum (not #9777 row)', () => {
		const sr = 48000;
		const frames = 2000 * RENDER_QUANTUM;
		const input = sineFixture({ sampleRate: sr, freq: 440, amp: 0.5, frames });
		const t0 = performance.now();
		render({
			sampleRate: sr,
			windowMs: 40,
			input,
			quanta: 2000,
			events: [{ atFrame: 0, depth: 0.5, active: 1, percent: 26 }]
		});
		const elapsed = performance.now() - t0;
		const usPerQuantum = (elapsed * 1000) / 2000;
		assert.ok(usPerQuantum < (RENDER_QUANTUM / sr) * 1e6);
		console.info(
			`pitch-fx CPU diagnostic: ${usPerQuantum.toFixed(1)} us/quantum (row waits on #9777)`
		);
	});
});
