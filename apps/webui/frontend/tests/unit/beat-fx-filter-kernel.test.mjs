// requirement: FXFILT-08
// real-audio counterpart (Chromium OfflineAudioContext): tests/e2e/beat-fx-filter-real-audio.spec.ts
// [if] a division outside 1/16..64 beats (or 3/8, 1/12) is accepted, or its error omits the value [then] broken
// [if] a missing tempo does not switch the kernel to TIME with reason no-tempo, or out-of-range TIME is accepted [then] broken
// [if] the cutoff is not a sine on the octave axis from the LP floor to the LP ceiling [then] broken
// [if] phase zero misses a downbeat by over 1 ms or the period misses the division by over 1 ms at 120 or 174 BPM [then] broken
// [if] setClock does not start one oscillator at the current phase and stop the previous one [then] broken
// [if] level 0 leaves dry gain other than exactly 1 or wet other than exactly 0 [then] broken
// [if] a level change creates an audio node [then] broken
// requirement: FXFILT-14361
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { readFrontendSource } from './engine-source.mjs';
import * as S from '../../src/lib/rb/fx/beat-fx-filter-lfo-shapes.ts';

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

	//---------------------------------------------------------------------------
	// LFO shapes and directions (#10014)

	const EIGHTHS = Array.from({ length: 8 }, (_, i) => i / 8);
	const R2 = Math.SQRT1_2;
	const near = (a, b, tol = 1e-12) => Math.abs(a - b) <= tol;
	const lfoClock = (beats, bpm, contextTime) =>
		K.resolveBeatFxFilterClock({
			beats,
			timeMs: 500,
			grid: { beats: grid(bpm, 2000), positionSec: 5.37, bpm },
			contextTime
		});
	/** One cycle of the oscillator's PeriodicWave, `points` samples from its start phase. */
	const waveTable = (wave, points = 8192) =>
		Float64Array.from({ length: points }, (_, i) => {
			let x = 0;
			for (let h = 1; h < wave.real.length; h++) {
				const w = (2 * Math.PI * h * i) / points;
				x += wave.real[h] * Math.cos(w) + wave.imag[h] * Math.sin(w);
			}
			return x;
		});

	test('shapes are sine, triangle, saw_decay and directions down, up; defaults sine down; 8 phases each on the octave axis', () => {
		// [if] a shape or direction name, a default or a trajectory at k/8 is off, or a bad name is accepted [then] the shape family is wrong, [else stop].
		assert.deepEqual([...S.FILTER_LFO_SHAPES], ['sine', 'triangle', 'saw_decay']);
		assert.deepEqual([...S.FILTER_LFO_DIRECTIONS], ['down', 'up']);
		assert.equal(S.FILTER_LFO_DEFAULT_SHAPE, 'sine');
		assert.equal(S.FILTER_LFO_DEFAULT_DIRECTION, 'down');
		const want = {
			sine: [-1, -R2, 0, R2, 1, R2, 0, -R2],
			triangle: [-1, -0.5, 0, 0.5, 1, 0.5, 0, -0.5],
			saw_decay: [-1, -0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75]
		};
		for (const shape of S.FILTER_LFO_SHAPES) {
			EIGHTHS.forEach((p, i) => {
				const unit = S.filterLfoUnit(shape, 'down', p);
				assert.ok(near(unit, want[shape][i]), `${shape} ${p}: ${unit}`);
				const hz = K.beatFxFilterCutoffHz(p, shape, 'down');
				assert.ok(
					near(Math.log2(hz / K.BEAT_FX_FILTER_CENTER_HZ), K.BEAT_FX_FILTER_SPAN_OCT * unit)
				);
			});
		}
		// saw_decay: the crest just before phase 0, a sharp fall through it, then the linear rise.
		assert.ok(S.filterLfoUnit('saw_decay', 'down', 1 - 1e-9) > 0.999);
		assert.equal(S.filterLfoUnit('saw_decay', 'down', 0, 0.02), 1);
		assert.ok(near(S.filterLfoUnit('saw_decay', 'down', 0.01, 0.02), 0));
		assert.equal(S.filterLfoUnit('saw_decay', 'down', 0.02, 0.02), -1);
		for (const bad of ['square', undefined]) {
			assert.throws(
				() => S.filterLfoUnit(bad, 'down', 0),
				(e) => e.message.includes(`got ${String(bad)}`)
			);
			for (const key of ['lfoShape', 'lfoDirection'])
				assert.throws(
					() => K.buildBeatFxFilterKernel(fakeContext(), { [key]: bad ?? null }),
					new RegExp(key)
				);
		}
	});

	test('defaults are bit-identical to the #9792 sine: cutoff law and oscillator wave', () => {
		// [if] the default or an explicit sine down differs by one bit from the #9792 cutoff or wave [then] the default moved, [else stop].
		for (let i = 0; i <= 200; i++) {
			const p = i / 100 - 0.5;
			const old =
				K.BEAT_FX_FILTER_CENTER_HZ * 2 ** (-K.BEAT_FX_FILTER_SPAN_OCT * Math.cos(2 * Math.PI * p));
			assert.ok(Object.is(K.beatFxFilterCutoffHz(p), old), `phase ${p}`);
			assert.ok(Object.is(K.beatFxFilterCutoffHz(p, 'sine', 'down'), old), `phase ${p}`);
		}
		const waves = [undefined, {}, { lfoShape: 'sine', lfoDirection: 'down' }].map((opts) => {
			const ctx = fakeContext();
			const kernel = K.buildBeatFxFilterKernel(ctx, opts);
			assert.deepEqual([kernel.lfoShape, kernel.lfoDirection], ['sine', 'down']);
			const clock = lfoClock(4, 120, 10);
			kernel.setClock(clock, 10);
			const phi = K.beatFxFilterPhase(clock, 10);
			const { wave } = ctx.oscillators[0];
			assert.equal(wave.real.length, 2);
			assert.ok(Object.is(wave.real[1], Math.fround(-Math.cos(2 * Math.PI * phi))));
			assert.ok(Object.is(wave.imag[1], Math.fround(Math.sin(2 * Math.PI * phi))));
			return wave;
		});
		assert.deepEqual(waves[1], waves[0]);
		assert.deepEqual(waves[2], waves[0]);
	});

	test('up mirrors each shape on the octave axis: down phase 0 at the floor, up phase 0 at the ceiling', () => {
		// [if] an up trajectory or its oscillator wave is not the octave mirror of down, or phase 0 sits wrong [then] direction is broken, [else stop].
		const rel = (a, b) => Math.abs(a - b) / b;
		const center = Math.log2(K.BEAT_FX_FILTER_CENTER_HZ);
		for (const shape of S.FILTER_LFO_SHAPES) {
			for (const p of EIGHTHS) {
				const down = Math.log2(K.beatFxFilterCutoffHz(p, shape, 'down')) - center;
				const up = Math.log2(K.beatFxFilterCutoffHz(p, shape, 'up')) - center;
				assert.ok(near(up, -down, 1e-9), `${shape} ${p}`);
			}
			assert.ok(rel(K.beatFxFilterCutoffHz(0, shape, 'down'), C.FILTER_LP_FLOOR_HZ) < 1e-9, shape);
			assert.ok(rel(K.beatFxFilterCutoffHz(0, shape, 'up'), C.FILTER_LP_CEILING_HZ) < 1e-9, shape);
			const [down, up] = ['down', 'up'].map((lfoDirection) => {
				const ctx = fakeContext();
				K.buildBeatFxFilterKernel(ctx, { lfoShape: shape, lfoDirection }).setClock(
					lfoClock(1, 128, 3),
					3
				);
				return ctx.oscillators[0].wave;
			});
			const neg = (a) => a.map((x) => 0 - x);
			assert.deepEqual([up.real, up.imag], [neg(down.real), neg(down.imag)], shape);
		}
	});

	test('saw_decay resets through the 10 ms ramp: click ratio at most 1.5, no NaN, no sample over 0 dBFS', () => {
		// [if] the reset beats the 10 ms ramp, clicks over 1.5, or a sample is NaN or over 0 dBFS (1/4, 1 beat; 120, 174 BPM) [then] broken, [else stop].
		const sr = 48000;
		// -3 dBFS, the pair export's fixture peak ceiling: the floor's corner hump (Q 0.707 dB) alone lifts a tone about 1.7 dB.
		const amp = 10 ** (-3 / 20);
		const toneHz = 997; // an instant reset peaks this tone at about +9.5 dBFS, the 10 ms ramp at about -1.4 dBFS
		const qLin = 10 ** (C.FILTER_Q_DB / 20); // Web Audio lowpass Q is in dB
		const maxSlope = (x, a, b) => {
			let m = 0;
			for (let i = Math.max(1, a); i < b; i++) m = Math.max(m, Math.abs(x[i] - x[i - 1]));
			return m;
		};
		for (const bpm of [120, 174]) {
			for (const beats of [1 / 4, 1]) {
				const ctx = fakeContext();
				const kernel = K.buildBeatFxFilterKernel(ctx, { lfoShape: 'saw_decay' });
				const now = 2;
				const clock = lfoClock(beats, bpm, now);
				kernel.setClock(clock, now);
				kernel.setLevel(1, now);
				const [osc] = ctx.oscillators;
				const table = waveTable(osc.wave);
				const resetPhase = K.beatFxFilterResetPhase(clock.periodSec);
				assert.ok(
					near(resetPhase * clock.periodSec, K.BEAT_FX_FILTER_LEVEL_RAMP_SEC, 1e-12),
					`${bpm} ${beats}`
				);
				// The oscillator's own fall from +0.9 to -0.9 lasts the raised cosine's 71 % of 10 ms.
				const lfoAt = (t) => {
					const x = (((t - osc.startedAt) * osc.frequency.value) % 1) * table.length;
					const i = Math.floor(x);
					return table[i] + (x - i) * (table[(i + 1) % table.length] - table[i]);
				};
				const resetSec = now + (1 - K.beatFxFilterPhase(clock, now)) * clock.periodSec;
				const cross = (level) => {
					let t = resetSec - 0.002;
					while (lfoAt(t) > level) t += 1e-5;
					return t;
				};
				const fallMs = (cross(-0.9) - cross(0.9)) * 1000;
				assert.ok(fallMs > 6.5 && fallMs < 7.8, `${bpm} ${beats} fall ${fallMs} ms`);
				// Render the wet path (level 1) sample by sample through a Web Audio lowpass biquad.
				const from = Math.max(now + 0.02, resetSec - 0.06);
				const frames = Math.round((resetSec + 0.04 - from) * sr);
				const input = Float64Array.from(
					{ length: frames },
					(_, i) => amp * Math.sin(2 * Math.PI * toneHz * (from + i / sr))
				);
				const out = new Float64Array(frames);
				let [x1, x2, y1, y2] = [0, 0, 0, 0];
				for (let i = 0; i < frames; i++) {
					const cents = lfoAt(from + i / sr) * kernel.lfoDepth.gain.value;
					const fc = kernel.lp.frequency.value * 2 ** (cents / 1200);
					const w0 = (2 * Math.PI * fc) / sr;
					const alpha = Math.sin(w0) / (2 * qLin);
					const a0 = 1 + alpha;
					const b1 = (1 - Math.cos(w0)) / a0;
					const y =
						(b1 / 2) * input[i] +
						b1 * x1 +
						(b1 / 2) * x2 -
						((-2 * Math.cos(w0)) / a0) * y1 -
						((1 - alpha) / a0) * y2;
					[x2, x1, y2, y1] = [x1, input[i], y1, y];
					out[i] = y;
				}
				assert.ok(out.every(Number.isFinite), `${bpm} ${beats} NaN`);
				const peak = out.reduce((m, y) => Math.max(m, Math.abs(y)), 0);
				assert.ok(peak <= 1, `${bpm} ${beats} peak ${peak}`);
				const at = Math.round((resetSec - from) * sr);
				const X = Math.round(K.BEAT_FX_FILTER_LEVEL_RAMP_SEC * sr);
				const ratio =
					maxSlope(out, at - X, at + 2 * X) /
					Math.max(maxSlope(out, at - 4 * X, at - X), maxSlope(input, at - X, at + 2 * X));
				assert.ok(ratio <= 1.5, `${bpm} ${beats} click ratio ${ratio}`);
			}
		}
	});
});
