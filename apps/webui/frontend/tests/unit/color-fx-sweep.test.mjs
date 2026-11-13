// requirement: COLORFX-14731
// [if] PARAMETER sweeps 0..1 [then] the centre maps 20 Hz .. 20 kHz on a log scale with 0.5 -> 632 Hz, [else stop].
// [if] COLOR turns CCW or CW [then] notch k rises K_MIN -> K_MAX, bandpass k falls K_MAX -> K_MIN, wet fades in over 30 percent, [else stop].
// [if] COLOR is full CCW [then] a sine at the centre is at least 40 dB down, [else stop].
// [if] COLOR is full CW [then] a sine an octave off the centre is 12 dB down and the centre is within 0.5 dB of dry, [else stop].
// [if] COLOR x PARAMETER walks the whole grid at 44.1 and 48 kHz [then] targets and render have no NaN or infinity, [else stop].
// [if] the SWEEP graph is built [then] it is closed and wired source -> notch/bandpass -> wet gains -> destination, [else stop].
import assert from 'node:assert/strict';
import { test } from 'node:test';

import * as sweep from '../../src/lib/rb/color-fx-sweep.ts';

const SR = 48000;

//--------------------------------------------------------------------- offline render (Web Audio biquad formulas)

/** Web Audio notch/bandpass coefficients (Audio EQ Cookbook, Q linear), normalised by a0. */
function biquadCoefficients(type, hz, q, sampleRate) {
	const f = Math.min(hz, sampleRate / 2);
	const w0 = (2 * Math.PI * f) / sampleRate;
	const alpha = Math.sin(w0) / (2 * q);
	const cos = Math.cos(w0);
	const a0 = 1 + alpha;
	const b = type === 'notch' ? [1, -2 * cos, 1] : [alpha, 0, -alpha];
	return { b: b.map((v) => v / a0), a: [(-2 * cos) / a0, (1 - alpha) / a0] };
}

function runBiquad(input, { b, a }) {
	const out = new Float64Array(input.length);
	let x1 = 0;
	let x2 = 0;
	let y1 = 0;
	let y2 = 0;
	for (let i = 0; i < input.length; i++) {
		const x = input[i];
		const y = b[0] * x + b[1] * x1 + b[2] * x2 - a[0] * y1 - a[1] * y2;
		x2 = x1;
		x1 = x;
		y2 = y1;
		y1 = y;
		out[i] = y;
	}
	return out;
}

/** The SWEEP mix at fixed targets: dry * x + notchWet * notch(x) + bandpassWet * bandpass(x). */
function renderSweep(input, targets, sampleRate = SR) {
	const notch = runBiquad(
		input,
		biquadCoefficients('notch', targets.centreHz, targets.notchQ, sampleRate)
	);
	const band = runBiquad(
		input,
		biquadCoefficients('bandpass', targets.centreHz, targets.bandpassQ, sampleRate)
	);
	return input.map(
		(x, i) => targets.dryGain * x + targets.notchWet * notch[i] + targets.bandpassWet * band[i]
	);
}

function sine(hz, seconds = 1, sampleRate = SR) {
	const n = Math.round(seconds * sampleRate);
	return Float64Array.from(
		{ length: n },
		(_, i) => 0.5 * Math.sin((2 * Math.PI * hz * i) / sampleRate)
	);
}

/** Steady-state RMS over the second half, so the filter transient is skipped. */
function tailRms(signal) {
	const start = Math.floor(signal.length / 2);
	let sum = 0;
	for (let i = start; i < signal.length; i++) sum += signal[i] * signal[i];
	return Math.sqrt(sum / (signal.length - start));
}

function levelDb(targets, hz) {
	const dry = sine(hz);
	return 20 * Math.log10(tailRms(renderSweep(dry, targets)) / tailRms(dry));
}

//--------------------------------------------------------------------- tests

test('PARAMETER maps 20 Hz .. 20 kHz on a log scale, 0.5 -> 632 Hz', () => {
	assert.equal(sweep.sweepCentreHz(0), sweep.SWEEP_FC_MIN_HZ);
	assert.ok(Math.abs(sweep.sweepCentreHz(1) - sweep.SWEEP_FC_MAX_HZ) < 1e-9);
	assert.equal(Math.round(sweep.sweepCentreHz(0.5)), 632);
	const ratio = sweep.sweepCentreHz(0.25) / sweep.sweepCentreHz(0);
	assert.ok(
		Math.abs(sweep.sweepCentreHz(0.5) / sweep.sweepCentreHz(0.25) - ratio) < 1e-9,
		'equal steps, equal ratios'
	);
});

test('CCW raises notch k, CW lowers bandpass k, wet crossfades over the first 30 percent', () => {
	const { SWEEP_K_MIN: kMin, SWEEP_K_MAX: kMax } = sweep;
	assert.deepEqual(
		[sweep.SWEEP_FC_MIN_HZ, sweep.SWEEP_FC_MAX_HZ, kMin, kMax],
		[20, 20000, 0.1, 2.0]
	);
	const near = (a, b) => Math.abs(a - b) < 1e-6;
	assert.ok(near(1 / sweep.sweepTargets(-1e-9, 0.5).notchQ, kMin));
	assert.ok(near(1 / sweep.sweepTargets(-1, 0.5).notchQ, kMax));
	assert.ok(near(1 / sweep.sweepTargets(1e-9, 0.5).bandpassQ, kMax));
	assert.ok(near(1 / sweep.sweepTargets(1, 0.5).bandpassQ, kMin));
	let lastNotchK = 0;
	let lastBandK = Infinity;
	for (let t = 0; t <= 1.0001; t += 0.05) {
		const ccw = sweep.sweepTargets(-t, 0.5);
		const cw = sweep.sweepTargets(t, 0.5);
		assert.ok(1 / ccw.notchQ >= lastNotchK && 1 / cw.bandpassQ <= lastBandK, `k monotonic at ${t}`);
		lastNotchK = 1 / ccw.notchQ;
		lastBandK = 1 / cw.bandpassQ;
		const wet = Math.min(1, t / 0.3);
		assert.ok(near(ccw.notchWet, wet) && ccw.bandpassWet === 0, `CCW wet at ${t}`);
		assert.ok(near(cw.bandpassWet, wet) && cw.notchWet === 0, `CW wet at ${t}`);
		assert.ok(near(ccw.dryGain, 1 - wet) && near(cw.dryGain, 1 - wet), `dry at ${t}`);
	}
	assert.deepEqual(
		[
			sweep.sweepTargets(0, 0.5).notchWet,
			sweep.sweepTargets(0, 0.5).bandpassWet,
			sweep.sweepTargets(0, 0.5).dryGain
		],
		[0, 0, 1]
	);
});

test('full CCW: a sine at the centre is at least 40 dB down', () => {
	for (const parameter of [0.25, 0.5, 0.75]) {
		const targets = sweep.sweepTargets(-1, parameter);
		const db = levelDb(targets, targets.centreHz);
		assert.ok(db <= -40, `centre ${targets.centreHz.toFixed(1)} Hz only ${db.toFixed(1)} dB down`);
	}
});

test('full CW: an octave off the centre is 12 dB down, the centre within 0.5 dB of dry', () => {
	for (const parameter of [0.4, 0.5, 0.6]) {
		const targets = sweep.sweepTargets(1, parameter);
		const centre = levelDb(targets, targets.centreHz);
		assert.ok(Math.abs(centre) <= 0.5, `centre at ${centre.toFixed(2)} dB`);
		for (const hz of [targets.centreHz * 2, targets.centreHz / 2]) {
			const db = levelDb(targets, hz);
			assert.ok(db <= -12, `${hz.toFixed(1)} Hz only ${db.toFixed(1)} dB down`);
		}
	}
});

test('no NaN or infinity anywhere on the COLOR x PARAMETER grid', () => {
	for (const sampleRate of [44100, 48000]) {
		const input = sine(437, 0.05, sampleRate);
		input[0] = 1;
		for (let c = -20; c <= 20; c++) {
			for (let p = 0; p <= 10; p++) {
				const targets = sweep.sweepTargets(c / 20, p / 10);
				assert.ok(Object.values(targets).every(Number.isFinite), `targets at ${c / 20}, ${p / 10}`);
				const out = renderSweep(input, targets, sampleRate);
				assert.ok(out.every(Number.isFinite), `render at ${c / 20}, ${p / 10}, ${sampleRate}`);
			}
		}
	}
	assert.throws(() => sweep.sweepTargets(Number.NaN, 0.5), /finite/);
});

//--------------------------------------------------------------------- node build

function fakeParam(value) {
	return {
		value,
		events: [],
		cancelScheduledValues(at) {
			this.events.push(['cancel', at]);
		},
		setValueAtTime(v, at) {
			this.events.push(['set', v, at]);
		},
		linearRampToValueAtTime(v, at) {
			this.events.push(['ramp', v, at]);
		}
	};
}

function fakeNode(kind) {
	return {
		kind,
		outputs: [],
		connect(dest) {
			this.outputs.push(dest);
		}
	};
}

const fakeCtx = {
	createGain: () => Object.assign(fakeNode('gain'), { gain: fakeParam(1) }),
	createBiquadFilter: () =>
		Object.assign(fakeNode('biquad'), {
			type: 'lowpass',
			frequency: fakeParam(350),
			Q: fakeParam(1)
		})
};

test('the SWEEP graph builds closed and wired source -> filters -> wets -> destination', () => {
	const source = fakeNode('high');
	const destination = fakeNode('colorBus');
	const graph = sweep.buildSweepGraph(fakeCtx, source, destination);
	assert.deepEqual([graph.notch.type, graph.bandpass.type], ['notch', 'bandpass']);
	assert.deepEqual(source.outputs, [graph.notch, graph.bandpass]);
	assert.deepEqual(graph.notch.outputs, [graph.notchWet]);
	assert.deepEqual(graph.bandpass.outputs, [graph.bandpassWet]);
	assert.deepEqual(graph.notchWet.outputs, [destination]);
	assert.deepEqual(graph.bandpassWet.outputs, [destination]);
	assert.deepEqual([graph.notchWet.gain.value, graph.bandpassWet.gain.value], [0, 0]);
	assert.equal(graph.nodes.length, 4);
	const dry = fakeParam(1);
	sweep.applySweepTargets(graph, dry, sweep.sweepTargets(1, 0.5), false, 2);
	assert.deepEqual(dry.events, [], 'dry untouched when SWEEP does not own it');
	assert.deepEqual(graph.bandpassWet.gain.events.at(-1), ['ramp', 1, 2 + sweep.SWEEP_RAMP_SEC]);
	sweep.applySweepTargets(graph, dry, sweep.sweepTargets(1, 0.5), true, 3);
	assert.deepEqual(dry.events.at(-1), ['ramp', 0, 3 + sweep.SWEEP_RAMP_SEC]);
});
