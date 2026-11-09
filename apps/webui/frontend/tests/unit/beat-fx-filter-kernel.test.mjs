// requirement: FXFILT-08
// real-audio counterpart (Chromium OfflineAudioContext): tests/e2e/beat-fx-filter-real-audio.spec.ts
// [if] a division outside 1/16..64 beats (or 3/8, 1/12) is accepted, or its error omits the value [then] broken
// [if] a missing tempo does not switch the kernel to TIME with reason no-tempo, or out-of-range TIME is accepted [then] broken
// [if] the cutoff is not a sine on the octave axis from the LP floor to the LP ceiling [then] broken
// [if] phase zero misses a downbeat by over 1 ms or the period misses the division by over 1 ms at 120 or 174 BPM [then] broken
// [if] setClock does not start one oscillator at the current phase and stop the previous one [then] broken
// [if] level 0 leaves dry gain other than exactly 1 or wet other than exactly 0 [then] broken
// [if] a level change creates an audio node [then] broken
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { readFrontendSource } from './engine-source.mjs';

const K = await loadTypeScriptModule('src/lib/rb/fx/beat-fx-filter-kernel.ts');
const C = await loadTypeScriptModule('src/lib/player/constants.ts');

//-----------------------------------------------------------------------------
// fake context

class Param {
	constructor(value = 0) {
		this.value = value;
		this.calls = [];
	}
	cancelAndHoldAtTime(t) {
		this.calls.push(['hold', t]);
	}
	setValueCurveAtTime(curve, t, d) {
		this.calls.push(['curve', Float32Array.from(curve), t, d]);
	}
}

class Node {
	constructor(kind) {
		this.kind = kind;
		this.links = [];
	}
	connect(dest) {
		this.links.push(dest);
		return dest;
	}
	disconnect() {
		this.links = [];
	}
}

function fakeContext() {
	const ctx = { created: 0, oscillators: [], waves: [] };
	ctx.createGain = () => (ctx.created++, Object.assign(new Node('gain'), { gain: new Param(1) }));
	ctx.createBiquadFilter = () => (
		ctx.created++,
		Object.assign(new Node('biquad'), {
			type: 'lowpass',
			frequency: new Param(350),
			Q: new Param(1),
			detune: new Param(0)
		})
	);
	ctx.createPeriodicWave = (real, imag, opts) => {
		ctx.created++;
		const wave = { real: Float32Array.from(real), imag: Float32Array.from(imag), opts };
		ctx.waves.push(wave);
		return wave;
	};
	ctx.createOscillator = () => {
		ctx.created++;
		const osc = Object.assign(new Node('osc'), {
			frequency: new Param(440),
			startedAt: null,
			stoppedAt: null,
			setPeriodicWave(w) {
				this.wave = w;
			},
			start(t) {
				this.startedAt = t;
			},
			stop(t = 0) {
				this.stoppedAt = t;
			}
		});
		ctx.oscillators.push(osc);
		return osc;
	};
	return ctx;
}

function grid(bpm, n = 200) {
	const spb = 60 / bpm;
	return Array.from({ length: n }, (_, i) => ({ n: (i % 4) + 1, bpm, t: i * spb }));
}

//-----------------------------------------------------------------------------

describe('beat FX filter kernel', () => {
	test('divisions: the twelve are accepted, anything else refused naming the value', () => {
		for (const b of [1 / 16, 1 / 8, 1 / 4, 1 / 2, 3 / 4, 1, 2, 4, 8, 16, 32, 64]) {
			assert.doesNotThrow(() => K.assertBeatFxFilterBeats(b));
		}
		for (const bad of [3 / 8, 0, 128, 1 / 12, NaN, undefined]) {
			assert.throws(
				() => K.assertBeatFxFilterBeats(bad),
				(e) => e.message.includes(`got ${String(bad)}`)
			);
		}
	});

	test('no usable tempo runs on TIME and says so; out-of-range TIME refused', () => {
		const g = grid(120);
		const noDownbeat = g.map((b) => ({ ...b, n: 2 }));
		for (const gridIn of [
			null,
			{ beats: g, positionSec: 1, bpm: null },
			{ beats: noDownbeat, positionSec: 1, bpm: 120 },
			{ beats: g, positionSec: 1, bpm: 320 }
		]) {
			const clock = K.resolveBeatFxFilterClock({
				beats: 1,
				timeMs: 500,
				grid: gridIn,
				contextTime: 2
			});
			assert.equal(clock.mode, 'time');
			assert.equal(clock.reason, 'no-tempo');
			assert.equal(clock.periodSec, 0.5);
			const kernel = K.buildBeatFxFilterKernel(fakeContext());
			kernel.setClock(clock, 2);
			assert.equal(kernel.state().mode, 'time');
			assert.equal(kernel.state().reason, 'no-tempo');
		}
		for (const bad of [9, 32001, NaN]) {
			for (const gridIn of [null, { beats: g, positionSec: 1, bpm: 120 }]) {
				assert.throws(
					() => K.resolveBeatFxFilterClock({ beats: 1, timeMs: bad, grid: gridIn, contextTime: 0 }),
					(e) => e.message.includes(`got ${String(bad)}`)
				);
			}
		}
	});

	test('cutoff is a sine on the octave axis of the LP FILTER law', () => {
		const rel = (a, b) => Math.abs(a - b) / b;
		assert.ok(rel(K.beatFxFilterCutoffHz(0), C.FILTER_LP_FLOOR_HZ) < 1e-9);
		assert.ok(rel(K.beatFxFilterCutoffHz(0.5), C.FILTER_LP_CEILING_HZ) < 1e-9);
		assert.ok(rel(K.beatFxFilterCutoffHz(0.25), K.BEAT_FX_FILTER_CENTER_HZ) < 1e-9);
		for (let i = 0; i <= 100; i++) {
			const p = i / 100;
			const want =
				Math.log2(K.BEAT_FX_FILTER_CENTER_HZ) -
				K.BEAT_FX_FILTER_SPAN_OCT * Math.cos(2 * Math.PI * p);
			assert.ok(Math.abs(Math.log2(K.beatFxFilterCutoffHz(p)) - want) < 1e-12);
		}
		const kernel = K.buildBeatFxFilterKernel(fakeContext());
		assert.equal(kernel.lfoDepth.gain.value, K.BEAT_FX_FILTER_SPAN_OCT * 1200);
		assert.equal(kernel.lp.frequency.value, K.BEAT_FX_FILTER_CENTER_HZ);
		assert.equal(kernel.lp.type, 'lowpass');
		assert.ok(kernel.lfoDepth.links.includes(kernel.lp.detune));
	});

	test('phase zero on the grid and period within 1 ms at 120 and 174 BPM', () => {
		for (const bpm of [120, 174]) {
			const spb = 60 / bpm;
			const beats = grid(bpm);
			const positionSec = 7.3 * spb; // mid-beat anchor
			const contextTime = 3.21;
			for (const div of K.BEAT_FX_FILTER_BEATS) {
				const clock = K.resolveBeatFxFilterClock({
					beats: div,
					timeMs: 500,
					grid: { beats, positionSec, bpm },
					contextTime
				});
				assert.equal(clock.mode, 'beat');
				assert.ok(Math.abs(clock.periodSec - (div * 60) / bpm) < 1e-3);
				// The first zero crossing after the anchor, in track time.
				const phase = K.beatFxFilterPhase(clock, contextTime);
				const zeroTrack = positionSec + (phase === 0 ? 0 : (1 - phase) * clock.periodSec);
				const zeroBeat = zeroTrack / spb;
				if (div >= 1) {
					const nearest = Math.round(zeroBeat);
					assert.ok(Math.abs(zeroBeat - nearest) * spb < 1e-3, `bpm ${bpm} div ${div}`);
					if (div % 4 === 0) assert.equal(beats[nearest].n, 1, `downbeat bpm ${bpm} div ${div}`);
					else assert.equal(nearest % div, 0, `cycle origin bpm ${bpm} div ${div}`);
				} else {
					const steps = zeroBeat / div;
					assert.ok(
						Math.abs(steps - Math.round(steps)) * div * spb < 1e-3,
						`bpm ${bpm} div ${div}`
					);
				}
				const later = K.beatFxFilterPhase(
					clock,
					contextTime + (zeroTrack - positionSec) + clock.periodSec * 3
				);
				assert.ok(Math.min(later, 1 - later) * clock.periodSec < 1e-3);
			}
		}
	});

	test('setClock starts one oscillator at the current phase and stops the previous one', () => {
		const ctx = fakeContext();
		const kernel = K.buildBeatFxFilterKernel(ctx);
		const beats = grid(120);
		const at = (bpm, t) =>
			K.resolveBeatFxFilterClock({
				beats: 4,
				timeMs: 500,
				grid: { beats, positionSec: 5.1, bpm },
				contextTime: t
			});
		const c1 = at(120, 10);
		kernel.setClock(c1, 10);
		const phi = K.beatFxFilterPhase(c1, 10);
		const [o1] = ctx.oscillators;
		assert.equal(o1.frequency.value, 1 / c1.periodSec);
		assert.equal(o1.startedAt, 10);
		assert.ok(Math.abs(o1.wave.real[1] - -Math.cos(2 * Math.PI * phi)) < 1e-6);
		assert.ok(Math.abs(o1.wave.imag[1] - Math.sin(2 * Math.PI * phi)) < 1e-6);
		assert.equal(o1.wave.opts.disableNormalization, true);
		assert.ok(o1.links.includes(kernel.lfoDepth));
		const c2 = at(126, 10);
		assert.ok(Math.abs(K.beatFxFilterPhase(c2, 10) - phi) < 1e-9);
		kernel.setClock(c2, 10);
		assert.equal(o1.stoppedAt, 10);
		assert.equal(ctx.oscillators.length, 2);
		assert.equal(kernel.state().bpm, 126);
	});

	test('level law: bit-exact dry at 0, raised-cosine ramps ending exactly on target', () => {
		const ctx = fakeContext();
		const kernel = K.buildBeatFxFilterKernel(ctx);
		assert.equal(kernel.dry.gain.value, 1);
		assert.equal(kernel.wet.gain.value, 0);
		assert.throws(() => kernel.setLevel(0.5, 0), /no clock/);
		for (const bad of [-0.1, 1.1, NaN])
			assert.throws(
				() => kernel.setLevel(bad, 0),
				(e) => e.message.includes(String(bad))
			);
		kernel.setClock(
			K.resolveBeatFxFilterClock({ beats: 1, timeMs: 500, grid: null, contextTime: 0 }),
			0
		);
		kernel.setLevel(1, 1);
		const wetCurve = kernel.wet.gain.calls.at(-1);
		const dryCurve = kernel.dry.gain.calls.at(-1);
		assert.deepEqual(
			[wetCurve[0], wetCurve[2], wetCurve[3]],
			['curve', 1, K.BEAT_FX_FILTER_LEVEL_RAMP_SEC]
		);
		assert.equal(wetCurve[1].at(-1), 1);
		assert.equal(dryCurve[1].at(-1), 0);
		assert.equal(wetCurve[1][0], 0);
		// Mid-ramp reversal starts from the raised-cosine value at that time.
		kernel.setLevel(0, 1.005);
		const back = kernel.wet.gain.calls.at(-1)[1];
		assert.ok(Math.abs(back[0] - 0.5) < 1e-6);
		assert.equal(back.at(-1), 0);
		assert.equal(kernel.dry.gain.calls.at(-1)[1].at(-1), 1);
		assert.equal(kernel.state().level, 0);
	});

	test('no per-quantum allocation: level moves create no nodes; a clock creates one oscillator and one wave', () => {
		const ctx = fakeContext();
		const kernel = K.buildBeatFxFilterKernel(ctx);
		kernel.setClock(
			K.resolveBeatFxFilterClock({ beats: 1, timeMs: 500, grid: null, contextTime: 0 }),
			0
		);
		const created = ctx.created;
		for (let i = 0; i < 1000; i++) kernel.setLevel((i % 10) / 10, i * 0.02);
		assert.equal(ctx.created, created);
		kernel.setClock(
			K.resolveBeatFxFilterClock({ beats: 2, timeMs: 500, grid: null, contextTime: 30 }),
			30
		);
		assert.equal(ctx.created, created + 2);
		const src = readFrontendSource('src/lib/rb/fx/beat-fx-filter-kernel.ts');
		for (const banned of [
			'createScriptProcessor',
			'AudioWorklet',
			'setInterval',
			'setTimeout',
			'requestAnimationFrame'
		]) {
			assert.ok(!src.includes(banned), banned);
		}
	});

	test('state reports beat mode with beats and bpm, then TIME after a tempo loss', () => {
		const kernel = K.buildBeatFxFilterKernel(fakeContext());
		assert.equal(kernel.state().mode, null);
		kernel.setClock(
			K.resolveBeatFxFilterClock({
				beats: 1 / 4,
				timeMs: 800,
				grid: { beats: grid(174), positionSec: 2, bpm: 174 },
				contextTime: 1
			}),
			1
		);
		assert.deepEqual(
			{ ...kernel.state(), periodSec: undefined },
			{
				mode: 'beat',
				reason: null,
				beats: 1 / 4,
				bpm: 174,
				timeMs: 800,
				periodSec: undefined,
				level: 0
			}
		);
		kernel.setClock(
			K.resolveBeatFxFilterClock({ beats: 1 / 4, timeMs: 800, grid: null, contextTime: 2 }),
			2
		);
		assert.equal(kernel.state().mode, 'time');
		assert.equal(kernel.state().reason, 'no-tempo');
		assert.equal(kernel.state().periodSec, 0.8);
	});
});
