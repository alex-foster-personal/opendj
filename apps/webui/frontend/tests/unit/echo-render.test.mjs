// requirement: FXECHO-10612
// Supplementary per-sample simulator (fast, node --test): delay lines as the cycle cut,
// RBJ biquads and waveshaper interpolation per the Web Audio spec, 48 kHz.
// real-audio acceptance (Chromium OfflineAudioContext): tests/e2e/echo-headroom-real-audio.spec.ts
// [if] the soft clip is bypassed and beat-locked full-scale bursts loop at 1 beat, feedback 0.95 [then] the output peaks over 0 dBFS (the known failure)
// [if] the guard is in and any fixture runs at any division or through a retune at feedback 0.95 [then] no output sample exceeds the 0.9 ceiling
// [if] the loop sum stays under -6 dBFS [then] the guarded render matches the pre-guard render within 1e-6
// [if] an impulse loops at 1/2 beat, feedback 0.55 [then] the first three repeats land on n * delay within 1 sample
// [if] the send stops [then] the tail decays at 0.55 below -60 dBFS within 4 s and still decays at 0.95 with the guard
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SR = 48000;
const BPM = 124;
const BEAT_SEC = 60 / BPM;
// Divisions the color FX offers (COLOR_FX_ECHO_BEATS). Only the ECHO mode is built; DELAY,
// PINGPONG and TAPE (FXECHO-01/02, #9780, #9814) join this matrix when they land.
const DIVISIONS = [0.25, 0.5, 0.75, 1];
// The color FX graph's loop filters (ECHO_LOOP_HP_HZ, ECHO_LOOP_LP_HZ).
const OPTS = { maxDelaySec: 2, loopHpHz: 180, loopLpHz: 3000 };

/** set + linear automation only, which is all the kernel schedules. */
class Param {
	constructor(value) {
		this.value = value;
		this.events = [];
	}
	setValueAtTime(v, t) {
		this.events.push({ kind: 'set', v, t });
	}
	linearRampToValueAtTime(v, t) {
		this.events.push({ kind: 'linear', v, t });
	}
	cancelScheduledValues(t) {
		this.events = this.events.filter((e) => e.t < t);
	}
	valueAt(t) {
		let val = this.value;
		let lastT = 0;
		for (const e of [...this.events].sort((a, b) => a.t - b.t)) {
			if (e.kind === 'set') {
				if (e.t > t) break;
				val = e.v;
				lastT = e.t;
			} else if (e.t <= t) {
				val = e.v;
				lastT = e.t;
			} else {
				return val + ((e.v - val) * (t - lastT)) / (e.t - lastT);
			}
		}
		return val;
	}
}

class Node {
	constructor(kind) {
		this.kind = kind;
	}
	connect(n) {
		return n;
	}
}

function renderContext() {
	return {
		currentTime: 0,
		sampleRate: SR,
		createGain() {
			const n = new Node('gain');
			n.gain = new Param(1);
			return n;
		},
		createDelay(max) {
			const n = new Node('delay');
			n.delayTime = new Param(0);
			n.buf = new Float64Array(Math.ceil(max * SR) + 2);
			return n;
		},
		createBiquadFilter() {
			const n = new Node('biquad');
			n.type = 'lowpass';
			n.frequency = new Param(350);
			n.Q = new Param(1);
			return n;
		},
		createWaveShaper() {
			const n = new Node('waveshaper');
			n.curve = null;
			n.oversample = 'none';
			return n;
		}
	};
}

/** RBJ cookbook, Q in dB as the Web Audio spec defines it for lowpass/highpass; Direct Form I. */
function biquad(node) {
	const w0 = (2 * Math.PI * node.frequency.value) / SR;
	const alpha = Math.sin(w0) / (2 * 10 ** (node.Q.value / 20));
	const cos = Math.cos(w0);
	const lp = node.type === 'lowpass';
	const b0 = lp ? (1 - cos) / 2 : (1 + cos) / 2;
	const b1 = lp ? 1 - cos : -(1 + cos);
	const a0 = 1 + alpha;
	const c = [b0 / a0, b1 / a0, b0 / a0, (-2 * cos) / a0, (1 - alpha) / a0];
	let x1 = 0;
	let x2 = 0;
	let y1 = 0;
	let y2 = 0;
	return (x) => {
		const y = c[0] * x + c[1] * x1 + c[2] * x2 - c[3] * y1 - c[4] * y2;
		x2 = x1;
		x1 = x;
		y2 = y1;
		y1 = y;
		return y;
	};
}

/** Web Audio WaveShaper with oversample none: clamp to the ends, linear between points. */
function shape(curve, x) {
	const n = curve.length;
	const v = ((n - 1) / 2) * (x + 1);
	if (v <= 0) return curve[0];
	if (v >= n - 1) return curve[n - 1];
	const k = Math.floor(v);
	const f = v - k;
	return (1 - f) * curve[k] + f * curve[k + 1];
}

/** Render `send` through a fresh kernel; `schedule(k)` sets the time(s); `bypass` skips the clip. */
function render(
	buildEchoKernel,
	send,
	{ feedback, schedule, seconds, bypass = false, filters = {} }
) {
	const k = buildEchoKernel(renderContext(), { ...OPTS, ...filters, feedback });
	schedule(k);
	const hp = biquad(k.loopHp);
	const lp = biquad(k.loopLp);
	const len = Math.round(seconds * SR);
	const out = new Float64Array(len);
	let loopPeak = 0;
	const [l0, l1] = k.lines;
	for (let n = 0; n < len; n++) {
		const t = n / SR;
		const tap = (line) => {
			const d = Math.round(line.delayTime.valueAt(t) * SR);
			const idx = n - d;
			return idx < 0 ? 0 : line.buf[idx % line.buf.length];
		};
		const y = k.lineGains[0].gain.valueAt(t) * tap(l0) + k.lineGains[1].gain.valueAt(t) * tap(l1);
		out[n] = y;
		// The delay lines are the only cycle cut, so feedback reaches the loop sum in the same sample.
		const fb = k.feedback.gain.value * lp(hp(y));
		const sum = (n < send.length ? send[n] : 0) + fb;
		loopPeak = Math.max(loopPeak, Math.abs(sum));
		const w = bypass ? sum : shape(k.softClip.curve, sum);
		l0.buf[n % l0.buf.length] = w;
		l1.buf[n % l1.buf.length] = w;
	}
	return { out, loopPeak };
}

// ----- fixtures (deterministic, no files) -----

/** Identical 80 ms 1 kHz decaying burst on every beat, peak `peak`: in phase at a 1-beat echo. */
function burstTrain(seconds, peak = 1) {
	const x = new Float64Array(Math.round(seconds * SR));
	const beat = Math.round(BEAT_SEC * SR);
	const burst = Math.round(0.08 * SR);
	for (let s = 0; s < x.length; s += beat) {
		for (let i = 0; i < burst && s + i < x.length; i++) {
			x[s + i] =
				peak * Math.exp(-i / (0.03 * SR)) * Math.sin((2 * Math.PI * 1000 * i) / SR + Math.PI / 2);
		}
	}
	return x;
}

function sineBurst() {
	const x = new Float64Array(Math.round(0.2 * SR));
	for (let i = 0; i < x.length; i++) x[i] = Math.sin((2 * Math.PI * 1000 * i) / SR);
	return x;
}

function noiseBurst() {
	const x = new Float64Array(Math.round(0.2 * SR));
	let s = 12345;
	for (let i = 0; i < x.length; i++) {
		s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
		x[i] = s / 2 ** 31 - 1;
	}
	return x;
}

const peakOf = (a, from = 0, to = a.length) => {
	let p = 0;
	let at = from;
	for (let i = from; i < to; i++) if (Math.abs(a[i]) > p) [p, at] = [Math.abs(a[i]), i];
	return { p, at };
};
const rms = (a, from, to) => {
	let s = 0;
	for (let i = from; i < to; i++) s += a[i] * a[i];
	return Math.sqrt(s / (to - from));
};
const at = (beats) => (k) => k.setTime(beats * BEAT_SEC, 0);

describe('echo-render', async () => {
	const { buildEchoKernel, ECHO_LOOP_CEILING, ECHO_LOOP_KNEE } = await loadTypeScriptModule(
		'src/lib/rb/fx/echo-kernel.ts'
	);

	test('known failure: pre-guard loop at feedback 0.95 goes over 0 dBFS', () => {
		const { out } = render(buildEchoKernel, burstTrain(3), {
			feedback: 0.95,
			schedule: at(1),
			seconds: 3,
			bypass: true
		});
		assert.ok(peakOf(out).p > 1, `fixture too cold to bite: peak ${peakOf(out).p}`);
	});

	test('guard: no sample over the ceiling on every fixture, division and a retune at 0.95', () => {
		const fixtures = { bursts: burstTrain(3), sine: sineBurst(), noise: noiseBurst() };
		const schedules = DIVISIONS.map((b) => [`${b} beat`, at(b)]);
		schedules.push([
			'retune 1/4 to 1',
			(k) => (k.setTime(0.25 * BEAT_SEC, 0), k.setTime(BEAT_SEC, 1.5))
		]);
		for (const [name, send] of Object.entries(fixtures)) {
			for (const [label, schedule] of schedules) {
				const { out } = render(buildEchoKernel, send, { feedback: 0.95, schedule, seconds: 3 });
				const { p, at: idx } = peakOf(out);
				assert.ok(p <= ECHO_LOOP_CEILING, `${name} ${label}: ${p} at sample ${idx}`);
			}
		}
	});

	test('identity below -6 dBFS: guarded equals pre-guard within 1e-6', () => {
		const send = burstTrain(2, 0.1);
		const args = { feedback: 0.55, schedule: at(1), seconds: 3 };
		const pre = render(buildEchoKernel, send, { ...args, bypass: true });
		assert.ok(pre.loopPeak < ECHO_LOOP_KNEE, `loop sum ${pre.loopPeak} reaches the knee`);
		const guarded = render(buildEchoKernel, send, args);
		let diff = 0;
		for (let i = 0; i < pre.out.length; i++)
			diff = Math.max(diff, Math.abs(guarded.out[i] - pre.out[i]));
		assert.ok(diff < 1e-6, `max abs diff ${diff}`);
	});

	test('repeat timing: first three repeats on n * delay within 1 sample', () => {
		const send = new Float64Array(1);
		send[0] = 0.25;
		// Loop filters opened up (1 Hz to 23 kHz) so their group delay does not move each peak.
		const filters = { loopHpHz: 1, loopLpHz: 23000 };
		const { out } = render(buildEchoKernel, send, {
			feedback: 0.55,
			schedule: at(0.5),
			seconds: 1.6,
			filters
		});
		const d = Math.round(0.5 * BEAT_SEC * SR);
		for (const n of [1, 2, 3]) {
			const { at: idx } = peakOf(out, n * d - (d >> 1), n * d + (d >> 1));
			assert.ok(Math.abs(idx - n * d) <= 1, `repeat ${n} at ${idx}, expected ${n * d}`);
		}
	});

	test('tail: decays below -60 dBFS at 0.55; still decays at 0.95 with the guard', () => {
		const bar = 4 * BEAT_SEC;
		const send = burstTrain(bar);
		const stop = send.length;
		const win = Math.round(BEAT_SEC * SR);
		// 1/4 beat: sixteen repeats per bar, so 4 s is enough loops to fall 60 dB at 0.55.
		const soft = render(buildEchoKernel, send, {
			feedback: 0.55,
			schedule: at(0.25),
			seconds: bar + 4
		}).out;
		let prev = Infinity;
		for (let s = stop + win; s + win <= soft.length; s += win) {
			const r = rms(soft, s, s + win);
			assert.ok(r <= prev + 1e-12, `tail rose at ${s}: ${r} > ${prev}`);
			prev = r;
		}
		assert.ok(
			peakOf(soft, soft.length - win).p < 10 ** (-60 / 20),
			'tail above -60 dBFS after 4 s'
		);
		const hot = render(buildEchoKernel, send, {
			feedback: 0.95,
			schedule: at(1),
			seconds: bar + 4
		}).out;
		const half = SR / 2;
		assert.ok(
			rms(hot, hot.length - half, hot.length) < rms(hot, stop, stop + half),
			'guard sustains the loop'
		);
	});
});
