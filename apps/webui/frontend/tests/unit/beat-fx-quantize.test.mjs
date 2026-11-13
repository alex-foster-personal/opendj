// requirement: BEATFX-10766
// requirement: BEATFX-10766b
// Supplementary per-sample offline simulator (node --test) of the beat FX send, the target's dry
// and the Release FX echo on the shared echo kernel, 48 kHz. The real-path render is
// tests/e2e/beat-fx-release-real-audio.spec.ts (AGENTS.md "Test doubles and locked real fixtures").
// [if] quantize is on with a grid and ON or OFF does not start within 2 ms of the next min(division, 1 beat) boundary, or lands more than a beat late [then] broken
// [if] quantize is on with no grid and the move does not start at once with state no_grid [then] broken
// [if] quantize is off and the plan is not off at once [then] broken
// [if] Release FX press leaves the dry open, the beat FX send open, its tail silent, or the release echo off its beat [then] broken
// [if] Release FX release does not restore the dry on the next boundary, or the release echo does not ring out and decay [then] broken
// [if] a release kind other than echo, a double press, a release when not held or an out-of-range bpm is accepted [then] broken
// [if] any quantized transition or Release FX press or release has click ratio over 1.5 [then] broken
// [if] any render has a non-finite, over 0 dBFS or subnormal sample [then] broken
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SR = 48000;
const BPM = 124;
const BEAT_SEC = 60 / BPM;
const GRID = Array.from({ length: 32 }, (_, k) => 0.1 + k * BEAT_SEC);
const TWO_MS = Math.round(0.002 * SR);

const Q = await loadTypeScriptModule('src/lib/rb/beat-fx-quantize.ts');
const { nextQuantizeBoundarySec, parseBeatFxReleaseCommand } =
	await loadTypeScriptModule('src/lib/rb/beat-fx.ts');
const { buildEchoKernel } = await loadTypeScriptModule('src/lib/rb/fx/echo-kernel.ts');
const { clickRatio, SLIP_RETURN_CLICK_VETO, assertBlendWindowClean } = await loadTypeScriptModule(
	'src/lib/rb/slip-return-blend.ts'
);

//----- simulator (set + linear automation, as echo-render.test.mjs)

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
	/** One value per frame, computed once per render. */
	curve(len) {
		const out = new Float64Array(len);
		const events = [...this.events].sort((a, b) => a.t - b.t);
		for (let n = 0; n < len; n++) out[n] = valueAt(this.value, events, n / SR);
		return out;
	}
}

function valueAt(value, events, t) {
	let val = value;
	let lastT = 0;
	for (const e of events) {
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

class Node {
	constructor() {
		this.edges = [];
	}
	connect(n) {
		this.edges.push(n);
		return n;
	}
	disconnect() {
		this.edges = [];
	}
}

function renderContext() {
	const gain = (kind) => Object.assign(new Node(), { kind, gain: new Param(1) });
	return {
		currentTime: 0,
		sampleRate: SR,
		createGain: () => gain('gain'),
		createChannelMerger: () => new Node(),
		createDelay(max) {
			return Object.assign(new Node(), {
				delayTime: new Param(0),
				buf: new Float64Array(Math.ceil(max * SR) + 2)
			});
		},
		createBiquadFilter() {
			return Object.assign(new Node(), {
				type: 'lowpass',
				frequency: new Param(350),
				Q: new Param(1)
			});
		},
		createWaveShaper() {
			return Object.assign(new Node(), { curve: null, oversample: 'none' });
		}
	};
}

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

function shape(curve, x) {
	const n = curve.length;
	const v = ((n - 1) / 2) * (x + 1);
	if (v <= 0) return curve[0];
	if (v >= n - 1) return curve[n - 1];
	const k = Math.floor(v);
	const f = v - k;
	return (1 - f) * curve[k] + f * curve[k + 1];
}

/** A per-sample stepper over a built kernel; delay times and line gains are precomputed curves. */
function kernelStepper(k, len) {
	const hp = biquad(k.loopHp);
	const lp = biquad(k.loopLp);
	const [l0, l1] = k.lines;
	const d0 = l0.delayTime.curve(len);
	const d1 = l1.delayTime.curve(len);
	const g0 = k.lineGains[0].gain.curve(len);
	const g1 = k.lineGains[1].gain.curve(len);
	const tap = (line, d, n) => {
		const idx = n - Math.round(d * SR);
		return idx < 0 ? 0 : line.buf[idx % line.buf.length];
	};
	return (n, send) => {
		const y = g0[n] * tap(l0, d0[n], n) + g1[n] * tap(l1, d1[n], n);
		const w = shape(k.softClip.curve, send + k.feedback.gain.value * lp(hp(y)));
		l0.buf[n % l0.buf.length] = w;
		l1.buf[n % l1.buf.length] = w;
		return y;
	};
}

//----- fixture and graph

/** Continuous 220 Hz sine plus a 1 kHz decaying burst on every grid beat. */
function fixture(len) {
	const x = new Float64Array(len);
	const burst = Math.round(0.08 * SR);
	for (let n = 0; n < len; n++) x[n] = 0.2 * Math.sin((2 * Math.PI * 220 * n) / SR);
	for (const b of GRID) {
		const s = Math.round(b * SR);
		for (let i = 0; i < burst && s + i < len; i++)
			x[s + i] += 0.2 * Math.exp(-i / (0.03 * SR)) * Math.sin((2 * Math.PI * 1000 * i) / SR);
	}
	return x;
}

/** The target, a 1/2-beat beat FX echo (stand-in for #9782's unit) and the Release FX. */
function buildGraph({ send = 0, dry = 1 } = {}) {
	const ctx = renderContext();
	const target = {
		dry: ctx.createGain(),
		beatFxSend: ctx.createGain(),
		tap: new Node(),
		out: new Node()
	};
	target.dry.gain.value = dry;
	target.beatFxSend.gain.value = send;
	const beatFx = buildEchoKernel(ctx, {
		mode: 'echo',
		maxDelaySec: 2,
		feedback: Q.BEAT_FX_RELEASE_FEEDBACK,
		loopHpHz: 180,
		loopLpHz: 3000
	});
	beatFx.setTime(BEAT_SEC / 2, 0);
	const release = Q.buildBeatFxRelease(ctx, target);
	return { ctx, target, beatFx, release };
}

/** Render the graph; returns the mix and its parts. */
function render(graph, seconds) {
	const len = Math.round(seconds * SR);
	const x = fixture(len);
	const dryG = graph.target.dry.gain.curve(len);
	const sendG = graph.target.beatFxSend.gain.curve(len);
	const relG = graph.release.releaseSend.gain.curve(len);
	const beatFx = kernelStepper(graph.beatFx, len);
	const rel = kernelStepper(graph.release.echo, len);
	const out = new Float32Array(len);
	const dry = new Float64Array(len);
	const beatWet = new Float64Array(len);
	const relWet = new Float64Array(len);
	for (let n = 0; n < len; n++) {
		dry[n] = x[n] * dryG[n];
		beatWet[n] = beatFx(n, x[n] * sendG[n]);
		relWet[n] = rel(n, x[n] * relG[n]);
		out[n] = dry[n] + beatWet[n] + relWet[n];
	}
	assertBlendWindowClean(out, 0, len);
	return { out, dry, beatWet, relWet, sendG, dryG, relG };
}

const firstDeparture = (curve, from) => {
	for (let n = from + 1; n < curve.length; n++) if (curve[n] !== curve[from]) return n;
	return -1;
};
const peakOf = (a, from, to) => {
	let p = 0;
	for (let n = from; n < to; n++) p = Math.max(p, Math.abs(a[n]));
	return p;
};

//----- tests

describe('beat fx quantized engage', () => {
	const cases = [];
	for (const beats of [1 / 16, 1 / 4, 3 / 4, 1, 4, 16])
		for (const off of [0.37, 2.91]) cases.push({ beats, nowSec: 0.1 + off * BEAT_SEC });

	test('ON and OFF land on the next min(division, 1 beat) boundary within 2 ms, never a beat late, click ratio <= 1.5', () => {
		const held0 = render(buildGraph({ send: 0 }), 4);
		const held1 = render(buildGraph({ send: 1 }), 4);
		for (const { beats, nowSec } of cases) {
			const graph = buildGraph({ send: 0 });
			const onPlan = Q.planQuantizedTransition({
				quantize: true,
				gridBeatsSec: GRID,
				nowSec,
				beats
			});
			assert.equal(onPlan.state, 'on');
			assert.equal(onPlan.atSec, nextQuantizeBoundarySec(GRID, nowSec, beats));
			const offNow = onPlan.atSec + 0.61 * BEAT_SEC;
			const offPlan = Q.planQuantizedTransition({
				quantize: true,
				gridBeatsSec: GRID,
				nowSec: offNow,
				beats
			});
			Q.scheduleBeatFxEnable(graph.target.beatFxSend, true, onPlan);
			Q.scheduleBeatFxEnable(graph.target.beatFxSend, false, offPlan);
			const r = render(graph, 4);
			for (const [plan, now, label] of [
				[onPlan, nowSec, 'ON'],
				[offPlan, offNow, 'OFF']
			]) {
				const step = Math.min(beats, 1) * BEAT_SEC;
				const beatStart = 0.1 + Math.floor((now - 0.1) / BEAT_SEC) * BEAT_SEC;
				const expected = Math.min(
					beatStart + Math.ceil((now - beatStart) / step - 1e-9) * step,
					beatStart + BEAT_SEC
				);
				const dep = firstDeparture(r.sendG, Math.floor(now * SR));
				assert.ok(
					Math.abs(dep - expected * SR) <= TWO_MS,
					`if ${label} at ${beats} beats departs at ${dep / SR} not ${expected} then broken`
				);
				assert.ok(
					plan.atSec - now <= BEAT_SEC + 1e-9,
					`if ${label} lands over a beat late then broken`
				);
				const ratio = clickRatio(r.out, [held0.out, held1.out], Math.round(plan.atSec * SR));
				assert.ok(
					ratio <= SLIP_RETURN_CLICK_VETO,
					`if ${label} ${beats} click ratio ${ratio} > 1.5 then broken`
				);
			}
		}
	});

	test('no grid engages at once with state no_grid', () => {
		for (const [grid, nowSec] of [
			[[], 0.5],
			[GRID, GRID[GRID.length - 1] + 0.2]
		]) {
			const plan = Q.planQuantizedTransition({
				quantize: true,
				gridBeatsSec: grid,
				nowSec,
				beats: 1
			});
			assert.deepEqual(plan, { state: 'no_grid', atSec: nowSec });
		}
		const graph = buildGraph({ send: 0 });
		Q.scheduleBeatFxEnable(graph.target.beatFxSend, true, { state: 'no_grid', atSec: 0.5 });
		const r = render(graph, 0.7);
		const dep = firstDeparture(r.sendG, Math.floor(0.5 * SR) - 1);
		assert.ok(
			Math.abs(dep - 0.5 * SR) <= 1,
			`if no_grid departs at ${dep} not at once then broken`
		);
	});

	test('quantize off is off at once', () => {
		const plan = Q.planQuantizedTransition({
			quantize: false,
			gridBeatsSec: GRID,
			nowSec: 0.83,
			beats: 1
		});
		assert.deepEqual(plan, { state: 'off', atSec: 0.83 });
	});
});

describe('Release FX echo', () => {
	test('buildBeatFxRelease wires tap -> releaseSend -> echo -> releaseReturn -> out', () => {
		const { target, release } = buildGraph({ send: 1 });
		assert.ok(
			target.tap.edges.includes(release.releaseSend),
			'if tap does not feed releaseSend then broken'
		);
		assert.ok(
			release.releaseSend.edges.includes(release.echo.input),
			'if releaseSend does not feed echo then broken'
		);
		const ret = release.echo.output.edges.find((n) => n.edges.includes(target.out));
		assert.ok(ret, 'if releaseReturn does not feed out then broken');
	});

	const t0 = 0.1 + 4.2 * BEAT_SEC;
	const beats = 1 / 2;
	const t1 = 0.1 + 6.3 * BEAT_SEC;
	// Continuous fixture: the held loop sits at its steady state, so the ring-out gets 5 s.
	const SECONDS = t1 + 5.5;

	const pressed = () => {
		const graph = buildGraph({ send: 1 });
		graph.release.press({ beats, bpm: BPM, plan: { state: 'off', atSec: t0 } });
		return graph;
	};

	test('press cuts the dry, closes the beat FX send (tail rings) and echoes on the beat', () => {
		const r = render(pressed(), t0 + 1);
		const after = Math.ceil((t0 + Q.BEAT_FX_RAMP_SEC) * SR);
		assert.equal(r.dryG[after], 0, 'if the dry is open after press then broken');
		assert.equal(r.sendG[after], 0, 'if the beat FX send is open after press then broken');
		assert.equal(r.relG[after], 1, 'if the release send is closed after press then broken');
		const from = Math.round((t0 + 0.02) * SR);
		const to = Math.round((t0 + beats * BEAT_SEC) * SR);
		assert.equal(peakOf(r.dry, from, to), 0, 'if the dry leaks while held then broken');
		assert.ok(
			peakOf(r.beatWet, from, to) > 0.01,
			'if the beat FX tail is cut on press then broken'
		);
		const first = r.relWet.findIndex((v) => Math.abs(v) > 0);
		const expected = Math.round(t0 * SR) + Math.round(beats * BEAT_SEC * SR) + 1;
		assert.ok(
			Math.abs(first - expected) <= 1,
			`if the release echo starts at ${first} not ${expected} then broken`
		);
	});

	test('release restores the dry on the next boundary and the release echo rings out', () => {
		const graph = pressed();
		const plan = Q.planQuantizedTransition({
			quantize: true,
			gridBeatsSec: GRID,
			nowSec: t1,
			beats
		});
		graph.release.release(plan);
		assert.equal(graph.release.held(), false);
		const r = render(graph, SECONDS);
		const expected = Math.round(nextQuantizeBoundarySec(GRID, t1, beats) * SR);
		const dep = firstDeparture(r.dryG, Math.floor(t1 * SR));
		assert.ok(
			Math.abs(dep - expected) <= TWO_MS,
			`if the dry returns at ${dep} not ${expected} then broken`
		);
		assert.equal(
			r.relG[dep + Q.BEAT_FX_RAMP_SEC * SR + 1],
			0,
			'if the release send stays open then broken'
		);
		const end = dep + Math.round(Q.BEAT_FX_RAMP_SEC * SR);
		const period = Math.round(beats * BEAT_SEC * SR);
		assert.ok(
			peakOf(r.relWet, end, end + period) > 0.01,
			'if the release echo stops at once then broken'
		);
		const tail = end + 5 * SR;
		assert.ok(
			peakOf(r.relWet, tail, r.relWet.length) < 1e-3,
			'if the release echo does not decay below -60 dBFS in 5 s then broken'
		);
	});

	test('press and release click ratio <= 1.5', () => {
		const before = render(buildGraph({ send: 1, dry: 1 }), SECONDS);
		const graph = pressed();
		const plan = Q.planQuantizedTransition({
			quantize: true,
			gridBeatsSec: GRID,
			nowSec: t1,
			beats
		});
		graph.release.release(plan);
		const r = render(graph, SECONDS);
		const heldOnly = render(pressed(), SECONDS);
		for (const [frame, sources, label] of [
			[Math.round(t0 * SR), [before.out, heldOnly.out], 'press'],
			[Math.round(plan.atSec * SR), [heldOnly.out, before.out], 'release']
		]) {
			const ratio = clickRatio(r.out, sources, frame);
			assert.ok(
				ratio <= SLIP_RETURN_CLICK_VETO,
				`if ${label} click ratio ${ratio} > 1.5 then broken`
			);
		}
	});

	test('refusals by name', () => {
		assert.throws(
			() => parseBeatFxReleaseCommand({ type: 'beat_fx_release', held: true, kind: 'reverb' }),
			(e) =>
				e instanceof TypeError &&
				/kind must be one of echo/.test(e.message) &&
				/reverb/.test(e.message)
		);
		const graph = buildGraph({ send: 1 });
		assert.throws(() => graph.release.release({ state: 'off', atSec: 0 }), /not held/);
		for (const bpm of [39, 301])
			assert.throws(
				() => graph.release.press({ beats, bpm, plan: { state: 'off', atSec: 0 } }),
				RangeError
			);
		graph.release.press({ beats, bpm: BPM, plan: { state: 'off', atSec: 0 } });
		assert.throws(
			() => graph.release.press({ beats, bpm: BPM, plan: { state: 'off', atSec: 0 } }),
			/already held/
		);
	});
});
