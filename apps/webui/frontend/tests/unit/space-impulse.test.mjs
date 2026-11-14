// requirement: FXVERB-14450
// SPACE impulse v2 (#9819): per-band decay, explicit energy normalisation, seeded determinism, graph wiring, tail.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { before, describe, test } from 'node:test';

import * as si from '../../src/lib/rb/space-impulse.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const RT60 = 2.6;
const RATES = [44100, 48000];

//--------------------------------------------------------------------- measures (as reverb-fdn.test.mjs)

/** RT60 from the Schroeder backward integral, a least-squares line over -5..-35 dB. */
function t30(h, sr) {
	const energy = new Float64Array(h.length + 1);
	for (let i = h.length - 1; i >= 0; i--) energy[i] = energy[i + 1] + h[i] * h[i];
	let [n, sx, sy, sxx, sxy] = [0, 0, 0, 0, 0];
	for (let i = 0; i < h.length; i += 16) {
		const db = 10 * Math.log10(energy[i] / energy[0]);
		if (db > -5 || db < -35) continue;
		const t = i / sr;
		[n, sx, sy, sxx, sxy] = [n + 1, sx + t, sy + db, sxx + t * t, sxy + t * db];
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

/** In-place iterative radix-2 FFT (inverse unscaled when `sign` is +1). */
function fft(re, im, sign = -1) {
	const n = re.length;
	for (let i = 1, j = 0; i < n; i++) {
		let bit = n >> 1;
		for (; j & bit; bit >>= 1) j ^= bit;
		j ^= bit;
		if (i < j) [re[i], re[j], im[i], im[j]] = [re[j], re[i], im[j], im[i]];
	}
	for (let size = 2; size <= n; size <<= 1) {
		const step = (sign * 2 * Math.PI) / size;
		for (let start = 0; start < n; start += size) {
			for (let k = 0; k < size / 2; k++) {
				const [c, s] = [Math.cos(step * k), Math.sin(step * k)];
				const [a, b] = [start + k, start + k + size / 2];
				const tr = re[b] * c - im[b] * s;
				const ti = re[b] * s + im[b] * c;
				[re[b], im[b]] = [re[a] - tr, im[a] - ti];
				[re[a], im[a]] = [re[a] + tr, im[a] + ti];
			}
		}
	}
}

function spectrum(h, n) {
	const re = new Float64Array(n);
	const im = new Float64Array(n);
	re.set(h);
	fft(re, im);
	return { re, im };
}

/** Linear convolution of `x` with `h` through one FFT of size `n` (n >= x + h). */
function convolve(x, h, n) {
	const X = spectrum(x, n);
	const H = spectrum(h, n);
	for (let k = 0; k < n; k++)
		[X.re[k], X.im[k]] = [
			X.re[k] * H.re[k] - X.im[k] * H.im[k],
			X.re[k] * H.im[k] + X.im[k] * H.re[k]
		];
	fft(X.re, X.im, 1);
	return X.re.map((v) => v / n);
}

/** Web Audio biquad |H|^2 at `f` (Q in dB, as the spec's lowpass and highpass read it). */
function biquadPower(type, hz, qDb, f, sr) {
	const w0 = (2 * Math.PI * hz) / sr;
	const alpha = Math.sin(w0) / (2 * 10 ** (qDb / 20));
	const cos = Math.cos(w0);
	const b1 = type === 'lowpass' ? 1 - cos : -(1 + cos);
	const [b0, a0, a1, a2] = [Math.abs(b1) / 2, 1 + alpha, -2 * cos, 1 - alpha];
	const w = (2 * Math.PI * f) / sr;
	const z = (c0, c1, c2) => [
		c0 + c1 * Math.cos(w) + c2 * Math.cos(2 * w),
		-(c1 * Math.sin(w) + c2 * Math.sin(2 * w))
	];
	const [nr, ni] = z(b0, b1, b0);
	const [dr, di] = z(a0, a1, a2);
	return (nr * nr + ni * ni) / (dr * dr + di * di);
}

/** Today's impulse: v1 noise (one envelope, seed 0xc010f) scaled as Web Audio `normalize = true` does. */
function todaysImpulse(sr, seconds) {
	const length = Math.round(sr * seconds);
	const ch = [new Float32Array(length), new Float32Array(length)];
	let state = 0xc010f;
	const next = () => {
		state ^= state << 13;
		state ^= state >>> 17;
		state ^= state << 5;
		return ((state >>> 0) / 0xffffffff) * 2 - 1;
	};
	let power = 0;
	for (let i = 0; i < length; i++) {
		const env = Math.exp((-Math.log(1000) / length) * i);
		ch[0][i] = next() * env;
		ch[1][i] = next() * env;
		power += ch[0][i] ** 2 + ch[1][i] ** 2;
	}
	const scale = (0.00125 * (44100 / sr)) / Math.sqrt(power / (2 * length));
	return ch.map((x) => x.map((v) => v * scale));
}

const sha256 = (channels) => {
	const hash = createHash('sha256');
	for (const x of channels) hash.update(Buffer.from(x.buffer, x.byteOffset, x.byteLength));
	return hash.digest('hex');
};
const energyPerChannel = (channels) =>
	channels.reduce((sum, x) => sum + x.reduce((s, v) => s + v * v, 0), 0) / channels.length;

//--------------------------------------------------------------------- tests

describe('space-impulse', () => {
	const built = {};
	before(() => {
		for (const sr of RATES) built[sr] = si.spaceImpulse(sr, RT60);
	});

	// [if] the impulse is built at RT60 2.6 s at 44.1 or 48 kHz [then] T30(500) is within 5% of 2.6 s and T30(4k)/T30(500) is 0.4..0.6, [else stop].
	test('per-band decay: highs die in about half the time of the lows', () => {
		for (const sr of RATES) {
			const band = (f) => built[sr].reduce((sum, h) => sum + t30(bandpass(h, f, sr), sr), 0) / 2;
			const [low, high] = [band(500), band(4000)];
			assert.ok(Math.abs(low / RT60 - 1) < 0.05, `${sr} Hz: T30(500) ${low.toFixed(3)} s`);
			assert.ok(
				high / low >= 0.4 && high / low <= 0.6,
				`${sr} Hz: T30(4k)/T30(500) ${(high / low).toFixed(3)}`
			);
		}
	});

	// [if] the impulse is built [then] energy per channel is 0.0689 x RT60 and pink, dark, bright full sends are within 1 dB of today, [else stop].
	test('explicit energy normalisation keeps the full-send wet level of today', () => {
		for (const sr of RATES) {
			const energy = energyPerChannel(built[sr]);
			assert.ok(
				Math.abs(energy / (si.SPACE_IMPULSE_ENERGY_PER_SEC * RT60) - 1) < 1e-5,
				`${sr} Hz energy ${energy}`
			);
			const n = 1 << 17;
			const power = (channels) => {
				const spectra = channels.map((h) => spectrum(h, n));
				return (weight) => {
					let sum = 0;
					for (let k = 1; k < n / 2; k++) {
						const f = (k * sr) / n;
						if (f < 20 || f > 20000) continue;
						for (const { re, im } of spectra) sum += weight(f) * (re[k] ** 2 + im[k] ** 2);
					}
					return sum;
				};
			};
			const [v2, today] = [power(built[sr]), power(todaysImpulse(sr, RT60))];
			const sends = {
				pink: (f) => 1 / f,
				dark: (f) => biquadPower('lowpass', 700, 1, f, sr) / f,
				bright: (f) => biquadPower('highpass', 2400, 1, f, sr) / f
			};
			for (const [name, weight] of Object.entries(sends)) {
				const db = 10 * Math.log10(v2(weight) / today(weight));
				assert.ok(Math.abs(db) <= 1, `${sr} Hz ${name} full send: ${db.toFixed(2)} dB re today`);
			}
		}
	});

	// [if] one seed and length build twice at 44.1 or 48 kHz [then] both match the pinned hash, another seed differs, bad input throws, [else stop].
	test('seeded and deterministic at both rates', () => {
		const pins = {
			44100: '042512f1922ca3a0647f8263a33a39d16a7d255b8f8ea90f3ac246da522af816',
			48000: '89314db64af60d66437fbdb54412c9b4119dd14c1534ed3091ca0c464e5b7bef'
		};
		for (const sr of RATES) {
			const [left, right] = built[sr];
			assert.equal(left.length, Math.round(sr * RT60));
			assert.equal(right.length, left.length);
			assert.equal(sha256(si.spaceImpulse(sr, RT60)), sha256(built[sr]));
			assert.equal(sha256(built[sr]), pins[sr], `${sr} Hz hash`);
			assert.notEqual(sha256(si.spaceImpulse(sr, RT60, si.SPACE_IMPULSE_SEED + 1)), pins[sr]);
		}
		assert.throws(() => si.spaceImpulse(0, RT60), /sampleRate 0 is not a positive rate/);
		assert.throws(() => si.spaceImpulse(48000, Number.NaN), /seconds NaN is not a positive RT60/);
	});

	// [if] color-fx-graph attaches SPACE [then] spaceImpulseBuffer(ctx) is the v2 impulse and normalize is false when it lands, [else stop].
	test('color-fx-graph builds the v2 impulse and sets normalize = false first', async () => {
		const graph = await loadTypeScriptModule('src/lib/rb/color-fx-graph.ts');
		const ctx = {
			sampleRate: 48000,
			createBuffer(channels, length, sampleRate) {
				const data = Array.from({ length: channels }, () => new Float32Array(length));
				return {
					length,
					sampleRate,
					numberOfChannels: channels,
					data,
					copyToChannel: (src, ch) => data[ch].set(src)
				};
			},
			createConvolver() {
				return {
					normalize: true,
					connect() {},
					set buffer(value) {
						this.normalizeWhenBufferSet = this.normalize;
						this.stored = value;
					},
					get buffer() {
						return this.stored;
					}
				};
			}
		};
		const buffer = graph.spaceImpulseBuffer(ctx);
		assert.equal(sha256(buffer.data), sha256(built[48000]));
		const host = { space: null, spaceIn: { connect() {} }, spaceReturn: {}, nodes: [] };
		const space = graph.attachSpace(ctx, host);
		assert.equal(space.normalize, false);
		assert.equal(
			space.normalizeWhenBufferSet,
			false,
			'normalize is read when the buffer is set, so it goes first'
		);
		assert.equal(sha256(space.buffer.data), sha256(built[48000]));
	});

	// [if] full-send noise stops on the 10 ms ramp [then] the wet is finite, peaks in 1 dB of today, is under -90 dB by 1.5 RT60 + 0.1 s, [else stop].
	test('tail: finite, bounded and gone within 1.5 x RT60 + 0.1 s of the send closing', () => {
		const sr = 44100;
		const [closeSec, tauSec] = [3, 0.01];
		const next = (() => {
			let s = 12345;
			return () => ((s = (s * 1103515245 + 12345) >>> 0) / 0xffffffff) * 2 - 1;
		})();
		const input = Float64Array.from({ length: Math.round(sr * (closeSec + 0.5)) }, (_, i) => {
			const t = i / sr;
			return 0.89 * next() * (t < closeSec ? 1 : Math.exp(-(t - closeSec) / tauSec));
		});
		const n = 1 << 19;
		const wet = (channels) => channels.map((h) => convolve(input, h, n));
		const [v2, today] = [wet(built[sr]), wet(todaysImpulse(sr, RT60))];
		const rms = (x, fromSec, toSec) => {
			let s = 0;
			const [a, b] = [Math.round(fromSec * sr), Math.round(toSec * sr)];
			for (let i = a; i < b; i++) s += x[i] * x[i];
			return Math.sqrt(s / (b - a));
		};
		const peak = (chs) =>
			Math.max(...chs.map((x) => x.reduce((m, v) => Math.max(m, Math.abs(v)), 0)));
		assert.ok(
			v2.every((x) => x.every(Number.isFinite)),
			'no NaN or infinity'
		);
		const peakDb = 20 * Math.log10(peak(v2) / peak(today));
		assert.ok(peakDb <= 1, `wet peak ${peakDb.toFixed(2)} dB re today`);
		const deadline = closeSec + 1.5 * RT60 + 0.1;
		for (const x of v2) {
			const steady = rms(x, closeSec - 0.5, closeSec);
			for (let t = deadline; t + 0.01 <= n / sr; t += 0.01) {
				const db = 20 * Math.log10(rms(x, t, t + 0.01) / steady + 1e-30);
				assert.ok(db < -90, `tail ${db.toFixed(1)} dB at ${t.toFixed(2)} s`);
			}
		}
	});
});
