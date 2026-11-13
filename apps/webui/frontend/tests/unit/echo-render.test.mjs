// requirement: FXECHO-10612
// Supplementary per-sample simulator (fast, node --test): delay lines as the cycle cut,
// RBJ biquads and waveshaper interpolation per the Web Audio spec, 48 kHz.
// real-audio acceptance (Chromium OfflineAudioContext): tests/e2e/echo-headroom-real-audio.spec.ts
// [if] the soft clip is bypassed and beat-locked full-scale bursts loop at 1 beat, feedback 0.95 [then] the output peaks over 0 dBFS (the known failure)
// [if] the guard is in and any fixture runs at any division or through a retune at feedback 0.95 [then] no output sample exceeds the 0.9 ceiling
// [if] the loop sum stays under -6 dBFS [then] the guarded render matches the pre-guard render within 1e-6
// [if] an impulse loops at 1/2 beat, feedback 0.55 [then] the first three repeats land on n * delay within 1 sample
// [if] the send stops [then] the tail decays at 0.55 below -60 dBFS within 4 s and still decays at 0.95 with the guard
// requirement: FXECHO-13830
// [if] an impulse runs through DELAY mode [then] it repeats exactly once at the time, [else stop].
// [if] an impulse runs through PING PONG [then] repeats land L at T, R at 1.5 T, L at 2 T, R at 2.5 T with the other side silent, [else stop].
// requirement: FXECHO-13945
// requirement: FXECHO-14147
// requirement: FXECHO-14240
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SR = 48000;
const BPM = 124;
const BEAT_SEC = 60 / BPM;
// Divisions the color FX offers (COLOR_FX_ECHO_BEATS). DELAY, PING PONG and TAPE render below.
const DIVISIONS = [0.25, 0.5, 0.75, 1];
// The color FX graph's loop filters (ECHO_LOOP_HP_HZ, ECHO_LOOP_LP_HZ).
const OPTS = { mode: 'echo', maxDelaySec: 2, loopHpHz: 180, loopLpHz: 3000 };

/** set, linear, exponential and setTarget automation: all the kernel schedules. `value` reads at the render clock, as Web Audio's getter does. */
class Param {
	constructor(value, clock) {
		this.base = value;
		this.clock = clock;
		this.events = [];
	}
	get value() {
		return this.valueAt(this.clock.t);
	}
	set value(v) {
		this.base = v;
	}
	setValueAtTime(v, t) {
		this.events.push({ kind: 'set', v, t });
	}
	linearRampToValueAtTime(v, t) {
		this.events.push({ kind: 'linear', v, t });
	}
	exponentialRampToValueAtTime(v, t) {
		this.events.push({ kind: 'exponential', v, t });
	}
	setTargetAtTime(v, t, tau) {
		this.events.push({ kind: 'target', v, t, tau });
	}
	cancelScheduledValues(t) {
		this.events = this.events.filter((e) => e.t < t);
	}
	valueAt(t) {
		let val = this.base;
		let lastT = 0;
		// An open setTarget: the value approaches `to` from `from` (its value at t0) with time constant tau.
		let glide = null;
		const glideAt = (at) =>
			glide.to + (glide.from - glide.to) * Math.exp(-(at - glide.t0) / glide.tau);
		for (const e of [...this.events].sort((a, b) => a.t - b.t)) {
			if (e.kind === 'set' || e.kind === 'target') {
				if (e.t > t) break;
				const from = glide ? glideAt(e.t) : val;
				glide = e.kind === 'target' ? { to: e.v, tau: e.tau, t0: e.t, from } : null;
				val = e.kind === 'set' ? e.v : from;
				lastT = e.t;
			} else if (e.t <= t) {
				val = e.v;
				lastT = e.t;
			} else {
				const frac = (t - lastT) / (e.t - lastT);
				return e.kind === 'exponential' ? val * (e.v / val) ** frac : val + (e.v - val) * frac;
			}
		}
		return glide ? glideAt(t) : val;
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

function renderContext(clock) {
	return {
		currentTime: 0,
		sampleRate: SR,
		createGain() {
			const n = new Node('gain');
			n.gain = new Param(1, clock);
			return n;
		},
		createDelay(max) {
			const n = new Node('delay');
			n.delayTime = new Param(0, clock);
			n.buf = new Float64Array(Math.ceil(max * SR) + 2);
			return n;
		},
		createBiquadFilter() {
			const n = new Node('biquad');
			n.type = 'lowpass';
			n.frequency = new Param(350, clock);
			n.Q = new Param(1, clock);
			return n;
		},
		createWaveShaper() {
			const n = new Node('waveshaper');
			n.curve = null;
			n.oversample = 'none';
			return n;
		},
		createChannelMerger() {
			return new Node('merger');
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

/**
 * Render `send` through a fresh kernel; `schedule(k)` sets the time(s) up front, `events` are
 * [seconds, (k) => ...] calls made when the render reaches them; `bypass` skips the clip;
 * `interp` reads fractional delays linearly; `options` go to buildEchoKernel.
 */
function render(
	buildEchoKernel,
	send,
	{
		feedback,
		schedule,
		seconds,
		bypass = false,
		filters = {},
		mode = 'echo',
		interp = false,
		events = [],
		options = {}
	}
) {
	const clock = { t: 0 };
	const k = buildEchoKernel(renderContext(clock), {
		...OPTS,
		...filters,
		...options,
		feedback,
		mode
	});
	schedule(k);
	const hp = biquad(k.loopHp);
	const lp = biquad(k.loopLp);
	const len = Math.round(seconds * SR);
	const out = new Float64Array(len);
	const left = new Float64Array(len);
	const right = new Float64Array(len);
	let loopPeak = 0;
	const [l0, l1] = k.lines;
	let due = 0;
	for (let n = 0; n < len; n++) {
		const t = n / SR;
		clock.t = t;
		while (due < events.length && n >= Math.round(events[due][0] * SR)) events[due++][1](k);
		const read = (line, idx) => (idx < 0 ? 0 : line.buf[idx % line.buf.length]);
		const tap = (line) => {
			const exact = line.delayTime.valueAt(t) * SR;
			if (!interp) return read(line, n - Math.round(exact));
			const d = Math.floor(exact);
			const f = exact - d;
			return (1 - f) * read(line, n - d) + f * read(line, n - d - 1);
		};
		const y = k.lineGains[0].gain.valueAt(t) * tap(l0) + k.lineGains[1].gain.valueAt(t) * tap(l1);
		out[n] = y;
		// Mono render: wet * y on both sides plus pingWet * the width mix of mono and its offset copy.
		const m = k.mono.gain.valueAt(t) * y;
		k.pong.buf[n % k.pong.buf.length] = m;
		const p = k.pongGain.gain.valueAt(t) * tap(k.pong);
		const [s0, s1, c0, c1] = [...k.straight, ...k.cross].map((g) => g.gain.valueAt(t));
		const [wet, ping] = [k.wet.gain.valueAt(t), k.pingWet.gain.valueAt(t)];
		left[n] = wet * y + ping * (s0 * m + c0 * p);
		right[n] = wet * y + ping * (c1 * m + s1 * p);
		// The delay lines are the only cycle cut, so feedback reaches the loop sum in the same sample.
		const h = hp(y);
		const fb =
			k.feedback.gain.valueAt(t) * lp(h) +
			k.tapeFeedback.gain.valueAt(t) * h +
			k.holdFeedback.gain.valueAt(t) * y;
		const x = n < send.length ? send[n] : 0;
		const sum = k.loopSum.gain.valueAt(t) * (k.input.gain.valueAt(t) * x + fb);
		loopPeak = Math.max(loopPeak, Math.abs(sum));
		const w = (bypass ? sum : shape(k.softClip.curve, sum)) * k.clipMakeup.gain.valueAt(t);
		l0.buf[n % l0.buf.length] = w;
		l1.buf[n % l1.buf.length] = w;
	}
	return { out, loopPeak, left, right, kernel: k };
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

describe('echo-render modes', async () => {
	const { buildEchoKernel } = await loadTypeScriptModule('src/lib/rb/fx/echo-kernel.ts');
	const T = 0.2;
	const impulse = Float64Array.of(0.5);
	const energy = (a, from) => rms(a, Math.round(from * SR), Math.round((from + 0.05) * SR));
	const run = (mode) =>
		render(buildEchoKernel, impulse, {
			feedback: 0.55,
			schedule: (k) => k.setTime(T, 0),
			seconds: 0.6,
			mode
		});

	test('DELAY repeats an impulse exactly once', () => {
		const { out } = run('delay');
		assert.equal(peakOf(out).at, Math.round(T * SR));
		assert.ok(Math.abs(peakOf(out).p - 0.5) < 1e-9);
		assert.equal(peakOf(out, Math.round(T * SR) + 1).p, 0, 'no second repeat');
		assert.ok(peakOf(run('echo').out, Math.round(1.5 * T * SR)).p > 0.01, 'echo repeats (control)');
	});

	test('PING PONG alternates L at T, R at 1.5 T, L at 2 T, R at 2.5 T', () => {
		const { left, right } = run('pingpong');
		assert.equal(peakOf(left).at, Math.round(T * SR));
		assert.equal(peakOf(right).at, Math.round(1.5 * T * SR));
		for (const [at, loud, quiet] of [
			[T, left, right],
			[1.5 * T, right, left],
			[2 * T, left, right],
			[2.5 * T, right, left]
		]) {
			assert.ok(energy(loud, at) > 1e-4, `energy at ${at}`);
			assert.equal(energy(quiet, at), 0, `other side silent at ${at}`);
		}
		for (const x of [...left, ...right]) assert.ok(Number.isFinite(x) && Math.abs(x) < 1);
	});
});

// ----- TAPE (#9814): glide, loop saturation, mode swap -----

/** `seconds` of an `hz` sine at `amp` with 20 ms raised-cosine fades at both ends. */
function fadedSine(seconds, amp = 0.5, hz = 1000) {
	const x = new Float64Array(Math.round(seconds * SR));
	const fade = Math.round(0.02 * SR);
	for (let i = 0; i < x.length; i++) {
		const edge = Math.min(i, x.length - 1 - i, fade) / fade;
		x[i] = amp * (0.5 - 0.5 * Math.cos(Math.PI * edge)) * Math.sin((2 * Math.PI * hz * i) / SR);
	}
	return x;
}

/** Peak second difference of `x` over [from, to) seconds. */
function maxSecondDiff(x, from, to) {
	let worst = 0;
	for (
		let i = Math.max(1, Math.round(from * SR));
		i < Math.min(x.length - 1, Math.round(to * SR));
		i++
	)
		worst = Math.max(worst, Math.abs(x[i + 1] - 2 * x[i] + x[i - 1]));
	return worst;
}

/** House click ratio: event-window second-difference peak over the larger steady peak 100 ms either side. */
function clickRatio(x, eventSec) {
	const event = maxSecondDiff(x, eventSec - 0.001, eventSec + 0.015);
	const steady = Math.max(
		maxSecondDiff(x, eventSec - 0.101, eventSec - 0.001),
		maxSecondDiff(x, eventSec + 0.015, eventSec + 0.115)
	);
	return event / steady;
}

/** Mean frequency (Hz) over [from, to) seconds from the first and last interpolated upward zero crossings. */
function frequencyOver(x, from, to) {
	const ups = [];
	for (let i = Math.round(from * SR) + 1; i < Math.round(to * SR); i++)
		if (x[i - 1] < 0 && x[i] >= 0) ups.push(i - 1 + x[i - 1] / (x[i - 1] - x[i]));
	return ((ups.length - 1) * SR) / (ups.at(-1) - ups[0]);
}

const cents = (hz, ref) => 1200 * Math.log2(hz / ref);

describe('echo-render tape', async () => {
	const kernel = await loadTypeScriptModule('src/lib/rb/fx/echo-kernel.ts');
	const {
		buildEchoKernel,
		ECHO_LOOP_CEILING,
		ECHO_LOOP_KNEE,
		ECHO_TAPE_DRIVE,
		ECHO_TAPE_GLIDE_SEC
	} = kernel;

	// [if] TAPE retunes on a 124 to 128 BPM step [then] repeat pitch moves, then sits within 5 cents after 4 glide constants, [else stop].
	test('tape retune bends repeat pitch, then settles within 5 cents after 4 time constants', () => {
		const [from, to, t0, tau] = [60 / 124, 60 / 128, 1, ECHO_TAPE_GLIDE_SEC];
		const { out, kernel: k } = render(buildEchoKernel, fadedSine(2), {
			feedback: 0,
			mode: 'tape',
			seconds: 1.8,
			interp: true,
			schedule: (kk) => (kk.setTime(from, 0), kk.setTime(to, t0))
		});
		const before = cents(frequencyOver(out, t0 - 0.05, t0 - 0.01), 1000);
		assert.ok(Math.abs(before) < 0.5, `control: ${before} cents before the retune`);
		const during = cents(frequencyOver(out, t0 + 0.005, t0 + 0.045), 1000);
		assert.ok(during > 50, `pitch did not move during the glide: ${during} cents`);
		const settled = cents(frequencyOver(out, t0 + 4 * tau, t0 + 4 * tau + 0.04), 1000);
		assert.ok(Math.abs(settled) <= 5, `${settled} cents after 4 time constants`);
		// The glide itself: no per-sample step beyond the initial slope, 98% of the way at 4 tau.
		const delay = (s) => k.lines[0].delayTime.valueAt(s);
		const slope = Math.abs(to - from) / (tau * SR);
		for (let n = Math.round(t0 * SR) - 8; n < Math.round((t0 + 0.6) * SR); n++) {
			const step = Math.abs(delay(n / SR) - delay((n - 1) / SR));
			assert.ok(step <= slope * 1.001, `delayTime step ${step} at sample ${n}`);
		}
		assert.ok(Math.abs(delay(t0 + 4 * tau) - to) <= 0.02 * Math.abs(to - from));
	});

	// [if] TAPE runs every fixture at feedback 0.95, also through a big glide [then] output is finite and under 0.9 / drive, [else stop].
	test('tape loop saturation: finite, bounded under the ceiling over the drive, and engaged', () => {
		const fixtures = { bursts: burstTrain(3), sine: sineBurst(), noise: noiseBurst() };
		const schedules = DIVISIONS.map((b) => [`${b} beat`, at(b)]);
		schedules.push([
			'glide 1/4 to 1',
			(k) => (k.setTime(0.25 * BEAT_SEC, 0), k.setTime(BEAT_SEC, 1.5))
		]);
		const bound = ECHO_LOOP_CEILING / ECHO_TAPE_DRIVE;
		for (const [name, send] of Object.entries(fixtures)) {
			for (const [label, schedule] of schedules) {
				const args = { feedback: 0.95, schedule, seconds: 3, mode: 'tape', interp: true };
				const { out } = render(buildEchoKernel, send, args);
				assert.ok(out.every(Number.isFinite), `${name} ${label}: non-finite sample`);
				const { p } = peakOf(out);
				assert.ok(p <= bound, `${name} ${label}: ${p} over ${bound}`);
				assert.ok(p > ECHO_LOOP_KNEE / ECHO_TAPE_DRIVE, `${name} ${label}: drive never bites`);
			}
		}
	});

	// [if] an impulse runs through TAPE at a fixed time [then] the first three repeats land on n * time within 1 sample, [else stop].
	test('tape repeat timing: first three repeats on n * delay within 1 sample', () => {
		const { out } = render(buildEchoKernel, Float64Array.of(0.2), {
			feedback: 0.55,
			schedule: at(0.5),
			seconds: 1.6,
			filters: { loopHpHz: 1 },
			mode: 'tape'
		});
		const d = Math.round(0.5 * BEAT_SEC * SR);
		for (const n of [1, 2, 3]) {
			const { at: idx, p } = peakOf(out, n * d - (d >> 1), n * d + (d >> 1));
			assert.ok(Math.abs(idx - n * d) <= 1, `repeat ${n} at ${idx}, expected ${n * d}`);
			assert.ok(Math.abs(p - 0.2 * 0.55 ** (n - 1)) < 0.01, `repeat ${n} level ${p}`);
		}
	});

	// [if] the mode swaps ECHO to TAPE and back mid-tail [then] the click ratio stays at or under 1.5 and the tail is kept, [else stop].
	test('tape and echo swap mid-tail without a click or a cut tail', () => {
		const T = 0.5;
		const base = { feedback: 0.55, seconds: 3.6, interp: true };
		const tone = fadedSine(1);
		const steady = render(buildEchoKernel, tone, { ...base, schedule: (k) => k.setTime(T, 0) }).out;
		const { out } = render(buildEchoKernel, tone, {
			...base,
			schedule: (k) => k.setTime(T, 0),
			events: [
				[1.75, (k) => k.setMode('tape', 1.75)],
				[2.75, (k) => k.setMode('echo', 2.75)]
			]
		});
		// Each swap and the moment its loop change comes out of the line, T later.
		for (const event of [1.75, 2.25, 2.75, 3.25]) {
			const ratio = clickRatio(out, event);
			assert.ok(ratio <= 1.5, `click ratio ${ratio} at ${event} s`);
		}
		for (const from of [2.3, 3.3]) {
			const [i, j] = [Math.round(from * SR), Math.round((from + 0.15) * SR)];
			const kept = rms(out, i, j) / rms(steady, i, j);
			assert.ok(kept > 0.5 && kept < 2, `tail at ${from} s is ${kept} of the echo-only tail`);
		}
	});
});

// ----- HELIX hold (#10002): endless loop, filter crossfade, release, retune while held -----

/** `seconds` of Paul Kellet's economy pink noise from a fixed LCG, scaled to peak `peak`. */
function pinkNoise(seconds, peak) {
	const x = new Float64Array(Math.round(seconds * SR));
	let [s, b0, b1, b2] = [987654321, 0, 0, 0];
	for (let i = 0; i < x.length; i++) {
		s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
		const white = s / 2 ** 31 - 1;
		b0 = 0.99765 * b0 + white * 0.099046;
		b1 = 0.963 * b1 + white * 0.2965164;
		b2 = 0.57 * b2 + white * 1.0526913;
		x[i] = b0 + b1 + b2 + white * 0.1848;
	}
	const top = peakOf(x).p;
	return x.map((v) => (v * peak) / top);
}

const dB = (a, b) => 20 * Math.log10(a / b);

describe('echo-render helix', async () => {
	const { buildEchoKernel, ECHO_LOOP_CEILING } = await loadTypeScriptModule(
		'src/lib/rb/fx/echo-kernel.ts'
	);
	const T = 0.5 * BEAT_SEC;
	const BAR = 4 * BEAT_SEC;
	const HELD = 16;
	// FXECHO tail bound (spec 04 section 6): ceil(90 / (-20 log10 loop gain)) + 2 repeats. The loop
	// biquads run at the Web Audio default Q of 1 dB, not Butterworth, so each peaks Q / sqrt(1 - 1 / 4Q^2).
	const Q = 10 ** (1 / 20);
	const LOOP_PEAK_GAIN = (0.55 * Q) / Math.sqrt(1 - 1 / (4 * Q * Q));
	const TAIL_REPEATS = Math.ceil(90 / (-20 * Math.log10(LOOP_PEAK_GAIN))) + 2;
	const repeatWindow = (start, n) => [
		Math.round((start + (n - 1) * T) * SR),
		Math.round((start + n * T) * SR)
	];
	/** One bar of pink noise at `peak`, hold 1 at the bar, released after HELD repeats, `after` repeats rendered past it. */
	const holdRender = (peak, after) =>
		render(buildEchoKernel, pinkNoise(BAR, peak), {
			feedback: 0.55,
			schedule: at(0.5),
			seconds: BAR + (HELD + after) * T,
			events: [
				[BAR, (k) => k.setHold(1, BAR)],
				[BAR + HELD * T, (k) => k.setHold(0, BAR + HELD * T)]
			]
		});
	const warm = holdRender(0.3, TAIL_REPEATS + 1);
	const hot = holdRender(1, 1);

	// [if] one bar of pink noise is held at 1/2 beat, 124 BPM [then] repeat 16 RMS is within 0.5 dB of repeat 2, no sample over 0 dBFS, [else stop].
	test('hold: the loop repeats endlessly at unity without clipping', () => {
		const level = (out, n) => rms(out, ...repeatWindow(BAR, n));
		const drift = dB(level(warm.out, HELD), level(warm.out, 2));
		assert.ok(Math.abs(drift) <= 0.5, `repeat ${HELD} is ${drift} dB from repeat 2`);
		assert.ok(level(warm.out, 2) > 0.02, 'held loop is not silent (control)');
		for (const { out } of [warm, hot]) {
			assert.ok(out.every(Number.isFinite), 'non-finite sample');
			assert.ok(peakOf(out).p <= ECHO_LOOP_CEILING, `peak ${peakOf(out).p} over the guard`);
		}
		// A full-scale send saturates the held loop down toward the knee, never up (no runaway).
		assert.ok(level(hot.out, HELD) <= level(hot.out, 2) * 1.0001, 'hot hold grows');
	});

	// [if] the hold is released [then] input, feedback and straight path return and the tail is under -90 dB within the FXECHO bound, [else stop].
	test('release: the loop decays under -90 dB within the tail bound', () => {
		const k = warm.kernel;
		const end = BAR + (HELD + 1) * T;
		assert.deepEqual(
			[k.input.gain.valueAt(end), k.feedback.gain.valueAt(end), k.holdFeedback.gain.valueAt(end)],
			[1, 0.55, 0]
		);
		const release = BAR + HELD * T;
		const [i, j] = repeatWindow(release, TAIL_REPEATS);
		const tail = peakOf(warm.out, i, j).p;
		assert.ok(tail < 10 ** (-90 / 20), `tail ${dB(tail, 1)} dB after ${TAIL_REPEATS} repeats`);
		assert.ok(peakOf(warm.out, ...repeatWindow(release, 2)).p > 0.01, 'tail starts (control)');
	});

	const H = 0.25;
	const tone = fadedSine(5, 0.2);
	const hz = (out, from, to) => cents(frequencyOver(out, from, to), 1000);

	// [if] a 1 kHz tone is held mid build-up and released 4 repeats later [then] each filter crossfade has click ratio at most 1.5, [else stop].
	test('hold and release crossfade the loop filter without a click', () => {
		const [hold, release] = [1.5 * H, 5.5 * H];
		const { out } = render(buildEchoKernel, tone, {
			feedback: 0.55,
			schedule: (k) => k.setTime(H, 0),
			seconds: release + 2 * H + 0.2,
			events: [
				[hold, (k) => k.setHold(1, hold)],
				[release, (k) => k.setHold(0, release)]
			]
		});
		// Each change and the moment it comes out of the line, one time later.
		for (const event of [hold, hold + H, release, release + H]) {
			const ratio = clickRatio(out, event);
			assert.ok(ratio <= 1.5, `click ratio ${ratio} at ${event} s`);
		}
	});

	// [if] the time moves 250 to 200 ms while a 1 kHz loop is held [then] pitch bends by default, stays within 5 cents with holdGlide false, [else stop].
	test('retune while held glides by default and crossfades with holdGlide false', () => {
		const [hold, retune] = [1.5, 2];
		const run = (options) =>
			render(buildEchoKernel, tone, {
				feedback: 0.55,
				schedule: (k) => k.setTime(H, 0),
				seconds: retune + 0.6,
				interp: true,
				options,
				events: [
					[hold, (k) => k.setHold(1, hold)],
					[retune, (k) => k.setTime(0.2, retune)]
				]
			}).out;
		const glide = run({});
		assert.ok(Math.abs(hz(glide, retune - 0.05, retune - 0.01)) < 0.5, 'held tone (control)');
		const bend = hz(glide, retune + 0.005, retune + 0.045);
		assert.ok(bend > 50, `held retune did not bend: ${bend} cents`);
		const cross = run({ holdGlide: false });
		for (const from of [retune + 0.005, retune + 0.1, retune + 0.4]) {
			const off = hz(cross, from, from + 0.04);
			assert.ok(Math.abs(off) <= 5, `${off} cents at ${from} s with holdGlide false`);
		}
	});
});

// ----- PING PONG width (#9992): stereo law, mono fold, width 0 equals ECHO, guard, clicks -----

describe('echo-render pingpong width', async () => {
	const { buildEchoKernel, ECHO_LOOP_CEILING } = await loadTypeScriptModule(
		'src/lib/rb/fx/echo-kernel.ts'
	);
	const T = 0.5 * BEAT_SEC;
	const impulse = Float64Array.of(0.25);
	const pingpong = (send, extra = {}) =>
		render(buildEchoKernel, send, {
			feedback: 0.55,
			schedule: at(0.5),
			seconds: 2,
			mode: 'pingpong',
			...extra
		});
	const fold = ({ left, right }) => left.map((v, i) => (v + right[i]) / 2);

	// [if] an impulse runs at 124 BPM, 1/2 beat, width 1 [then] repeats 1 to 6 alternate L, R within 2 ms, the other side under -40 dBFS, [else stop].
	test('width 1: repeats 1 to 6 alternate L and R on their half-time slots', () => {
		const { left, right } = pingpong(impulse);
		const half = Math.round((T / 4) * SR);
		for (let n = 1; n <= 6; n++) {
			const slot = Math.round((1 + (n - 1) / 2) * T * SR);
			const [loud, quiet] = n % 2 ? [left, right] : [right, left];
			const { at: idx, p } = peakOf(loud, slot - half, slot + half);
			assert.ok(Math.abs(idx - slot) <= 0.002 * SR, `repeat ${n} at ${idx}, slot ${slot}`);
			assert.ok(p > 0.01, `repeat ${n} is ${p} (control)`);
			const other = peakOf(quiet, slot - half, slot + half).p;
			assert.ok(other < 10 ** (-40 / 20), `repeat ${n} other side ${dB(other, 1)} dBFS`);
		}
	});

	// [if] PING PONG runs at the default width with no options [then] L is ECHO's line bit for bit and R's tail RMS (T / 2 later) is ECHO's within 0.05 dB, main's level, [else stop].
	test('default width: each side plays at main PING PONG level, no 3 dB lift', () => {
		// R is the line T / 2 late: its window is ECHO's, shifted by that offset.
		const [from, to, half] = [
			Math.round(T * SR) - 1,
			Math.round(1.5 * SR),
			Math.round((T / 2) * SR)
		];
		for (const send of [impulse, noiseBurst().map((v) => 0.1 * v), burstTrain(2)]) {
			const echo = pingpong(send, { mode: 'echo' }).out;
			const { left, right } = pingpong(send);
			let diff = 0;
			for (let i = 0; i < echo.length; i++) diff = Math.max(diff, Math.abs(left[i] - echo[i]));
			assert.equal(diff, 0, 'L is the ECHO line, as on main');
			const r = dB(rms(right, from + half, to + half), rms(echo, from, to));
			assert.ok(Math.abs(r) <= 0.05, `R tail ${r} dB from ECHO`);
			assert.ok(peakOf(echo).p > 0.01, 'echo renders (control)');
		}
	});

	// [if] the width is 0.5 [then] each side takes 3/4 of its own line and 1/4 of the other, the right line T / 4 late, [else stop].
	test('width 0.5: offset and channel mix move linearly', () => {
		const { left, right, kernel: k } = pingpong(impulse, { options: { pingpongWidth: 0.5 } });
		const a = Math.round(T * SR);
		const b = a + Math.round((T / 4) * SR);
		const m = 0.25 * k.mono.gain.value;
		const got = [left[a], left[b], right[a], right[b]];
		const want = [0.75 * m, 0.25 * m, 0.25 * m, 0.75 * m];
		for (const [i, v] of got.entries())
			assert.ok(Math.abs(v - want[i]) < 1e-12, `${got} vs ${want}`);
	});

	// [if] an impulse and a quiet noise burst run at width 1 with the opt-in makeup [then] the mono fold tail RMS is within 0.5 dB of ECHO's, [else stop].
	test('mono fold: width 1 with the makeup keeps the ECHO tail RMS within 0.5 dB, width 0.5 follows the law', () => {
		const fixtures = { impulse, noise: noiseBurst().map((v) => 0.1 * v) };
		const [from, to] = [Math.round(T * SR) - 1, 2 * SR];
		for (const [name, send] of Object.entries(fixtures)) {
			const echo = rms(pingpong(send, { mode: 'echo' }).out, from, to);
			const level = (options) =>
				dB(
					rms(fold(pingpong(send, { options: { pingpongMakeup: true, ...options } })), from, to),
					echo
				);
			const full = level({});
			assert.ok(Math.abs(full) <= 0.5, `${name}: mono fold ${full} dB from ECHO`);
			const mid = level({ pingpongWidth: 0.5 });
			assert.ok(Math.abs(mid - 10 * Math.log10(0.75)) <= 0.5, `${name}: width 0.5 fold ${mid} dB`);
			const unity = level({ pingpongMakeup: false });
			assert.ok(unity < -2.5, `${name}: unity makeup fold ${unity} dB (control)`);
		}
	});

	// [if] the width is 0 on an impulse, bursts and noise, through a retune [then] L and R are bit-identical to ECHO, [else stop].
	test('width 0 renders bit-identical to ECHO', () => {
		const retune = [[1.2, (k) => k.setTime(0.25 * BEAT_SEC, 1.2)]];
		for (const send of [impulse, burstTrain(2), noiseBurst()]) {
			const args = { seconds: 2.5, events: retune };
			const echo = pingpong(send, { ...args, mode: 'echo' });
			const zero = pingpong(send, { ...args, options: { pingpongWidth: 0 } });
			let diff = 0;
			for (let i = 0; i < echo.left.length; i++) {
				diff = Math.max(diff, Math.abs(zero.left[i] - echo.left[i]));
				diff = Math.max(diff, Math.abs(zero.right[i] - echo.right[i]));
			}
			assert.equal(diff, 0);
			assert.ok(peakOf(echo.left).p > 0.1, 'echo renders (control)');
		}
	});

	// [if] every fixture runs at width 1, feedback 0.95, each division and through a switch, makeup off and on [then] L and R are finite and under 0.9, [else stop].
	test('width 1 at feedback 0.95: finite and under the guard ceiling, also through a switch', () => {
		const fixtures = { bursts: burstTrain(3), sine: sineBurst(), noise: noiseBurst() };
		const cases = DIVISIONS.map((b) => [`${b} beat`, { schedule: at(b) }]);
		cases.push([
			'echo to pingpong',
			{ mode: 'echo', events: [[1.5, (k) => k.setMode('pingpong', 1.5)]] }
		]);
		cases.push([
			'width 0 to 1',
			{ options: { pingpongWidth: 0 }, events: [[1.5, (k) => k.setWidth(1, 1.5)]] }
		]);
		for (const [name, send] of Object.entries(fixtures)) {
			for (const [label, extra] of cases) {
				for (const pingpongMakeup of [false, true]) {
					const options = { pingpongMakeup, ...extra.options };
					const run = { feedback: 0.95, seconds: 3, ...extra, options };
					const { left, right } = pingpong(send, run);
					for (const side of [left, right]) {
						const tag = `${name} ${label} makeup ${pingpongMakeup}`;
						assert.ok(side.every(Number.isFinite), `${tag}: non-finite sample`);
						const { p, at: idx } = peakOf(side);
						assert.ok(p <= ECHO_LOOP_CEILING, `${tag}: ${p} at sample ${idx}`);
					}
				}
			}
		}
	});

	// [if] the width moves 1 to 0 and back mid-tail on a 1 kHz tone [then] both sides keep a click ratio at or under 1.5, [else stop].
	test('width changes mid-tail are click free on both sides', () => {
		const { left, right } = pingpong(fadedSine(1), {
			seconds: 3.6,
			interp: true,
			options: { pingpongMakeup: true },
			schedule: (k) => k.setTime(0.5, 0),
			events: [
				[1.75, (k) => k.setWidth(0, 1.75)],
				[2.75, (k) => k.setWidth(1, 2.75)]
			]
		});
		// Each change, and the makeup rise one line time after the widening.
		for (const event of [1.75, 2.75, 3.25]) {
			for (const [side, x] of [
				['L', left],
				['R', right]
			]) {
				const ratio = clickRatio(x, event);
				assert.ok(ratio <= 1.5, `${side} click ratio ${ratio} at ${event} s`);
			}
		}
	});
});
