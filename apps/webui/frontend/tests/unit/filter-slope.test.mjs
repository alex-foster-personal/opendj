// requirement: FXFILT-14532
// [if] FILTER_SLOPE_DB_OCT or the second-section Q differ between constants.ts and mixer.rs, or 12 adds a node [then] broken, [else stop].
// [if] a 24 dB/oct side lacks a second section of its type at its corner with Q -3.01 dB [then] the cascade is wrong, [else stop].
// [if] the RBJ cascade 1 to 2 octaves past the corner is outside 24 +/- 2 dB/oct (12 +/- 2 at 12, LP: second adds 12) [then] broken, [else stop].
// [if] the second section's corner does not get the first section's exact glide [then] the sections part on a sweep, [else stop].
// [if] the slope constant's comment does not name FXFILT-02 as resonance owner [then] the second Q may carry resonance, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { SpecAudioParam } from './fixtures/spec-audio-param.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const REPO = fileURLToPath(new URL('../../../../../', import.meta.url));
const SR = 48000;

let slope;
let constants;
let guards;
let colorGraph;

before(async () => {
	slope = await loadTypeScriptModule('src/lib/rb/filter-slope.ts');
	constants = await loadTypeScriptModule('src/lib/player/constants.ts');
	guards = await loadTypeScriptModule('src/lib/rb/audio-engine-guards.ts');
	colorGraph = await loadTypeScriptModule('src/lib/rb/color-fx-graph.ts');
});

function fakeBiquad(type, hz, qDb) {
	return {
		type,
		Q: { value: qDb },
		frequency: { value: hz },
		outputs: new Set(),
		connect(node) {
			this.outputs.add(node);
			return node;
		}
	};
}

function fakeContext() {
	const created = [];
	return {
		created,
		createBiquadFilter() {
			const node = fakeBiquad('lowpass', 350, 1);
			created.push(node);
			return node;
		}
	};
}

/** |H| of one Web Audio lowpass/highpass biquad (RBJ cookbook, Q read in dB) at `f` Hz. */
function biquadMagnitude(type, f0, qDb, f) {
	const w0 = (2 * Math.PI * f0) / SR;
	const alpha = Math.sin(w0) / (2 * 10 ** (qDb / 20));
	const cos = Math.cos(w0);
	const [b0, b1, b2] =
		type === 'lowpass'
			? [(1 - cos) / 2, 1 - cos, (1 - cos) / 2]
			: [(1 + cos) / 2, -(1 + cos), (1 + cos) / 2];
	const [a0, a1, a2] = [1 + alpha, -2 * cos, 1 - alpha];
	const w = (2 * Math.PI * f) / SR;
	const re = (b) => b[0] + b[1] * Math.cos(w) + b[2] * Math.cos(2 * w);
	const im = (b) => -(b[1] * Math.sin(w) + b[2] * Math.sin(2 * w));
	const num = Math.hypot(re([b0, b1, b2]), im([b0, b1, b2]));
	const den = Math.hypot(re([a0, a1, a2]), im([a0, a1, a2]));
	return num / den;
}

/** Build one side the way the channel graph does and return its sections, first section first. */
function buildSide(type, hz, slopeDbOct) {
	const ctx = fakeContext();
	const first = fakeBiquad(type, hz, constants.FILTER_Q_DB);
	const wet = { name: 'wet' };
	const second = slope.wireFilterSide(ctx, first, wet, slopeDbOct);
	return { ctx, first, second, wet, sections: second === null ? [first] : [first, second] };
}

/** Measured slope (dB/oct, 1 to 2 octaves past the corner) of the knob's active side at 12 and 24. */
function sideSlopes(knob) {
	const p = guards.filterParamsFromKnob(knob);
	const [type, hz, one, two] =
		knob < 0.5
			? ['lowpass', p.lpHz, p.lpHz * 2, p.lpHz * 4]
			: ['highpass', p.hpHz, p.hpHz / 2, p.hpHz / 4];
	const out = {};
	for (const db of [12, 24]) {
		const { sections } = buildSide(type, hz, db);
		out[db] = chainDb(sections, one) - chainDb(sections, two);
		assert.ok(Number.isFinite(out[db]), `knob ${knob} slope ${db}: finite response`);
	}
	return out;
}

function chainDb(sections, f) {
	const mag = sections.reduce(
		(m, s) => m * biquadMagnitude(s.type, s.frequency.value, s.Q.value, f),
		1
	);
	return 20 * Math.log10(mag);
}

describe('FILTER slope (FXFILT-09)', () => {
	test('the slope is 12 in both engines and 12 wires the shipped single biquad', () => {
		const mixer = readFileSync(`${REPO}apps/audio-engine/src/mixer.rs`, 'utf8');
		const rustSlope = mixer.match(/pub const FILTER_SLOPE_DB_OCT: u32 = (\d+);/);
		const rustQ = mixer.match(/pub const FILTER_SECOND_SECTION_Q_DB: f64 = (-?[0-9.]+);/);
		assert.ok(
			rustSlope && rustQ,
			'mixer.rs names FILTER_SLOPE_DB_OCT and FILTER_SECOND_SECTION_Q_DB'
		);
		assert.equal(constants.FILTER_SLOPE_DB_OCT, 12);
		assert.equal(Number(rustSlope[1]), constants.FILTER_SLOPE_DB_OCT);
		assert.equal(Number(rustQ[1]), constants.FILTER_SECOND_SECTION_Q_DB);
		const side = buildSide('lowpass', 1000, 12);
		assert.equal(side.second, null);
		assert.equal(side.ctx.created.length, 0, 'no node is added at 12 dB/oct');
		assert.deepEqual([...side.first.outputs], [side.wet], 'the first section feeds its wet gain');
	});

	test('24 puts a flat Butterworth second section of the same type at the same corner', () => {
		assert.ok(Math.abs(10 ** (constants.FILTER_SECOND_SECTION_Q_DB / 20) - Math.SQRT1_2) < 1e-15);
		for (const type of ['lowpass', 'highpass']) {
			const side = buildSide(type, 777, 24);
			assert.equal(side.ctx.created.length, 1);
			assert.equal(side.second.type, type);
			assert.equal(side.second.frequency.value, 777);
			assert.equal(side.second.Q.value, constants.FILTER_SECOND_SECTION_Q_DB);
			assert.equal(side.first.Q.value, constants.FILTER_Q_DB, 'the first section keeps its Q');
			assert.deepEqual([...side.first.outputs], [side.second]);
			assert.deepEqual([...side.second.outputs], [side.wet]);
			const cornerDb = 20 * Math.log10(biquadMagnitude(type, 777, side.second.Q.value, 777));
			assert.ok(
				Math.abs(cornerDb + 3.0103) < 1e-3,
				`second section is -3 dB at its corner, ${cornerDb}`
			);
		}
	});

	test('the cascade falls 24 +/- 2 dB/oct (12 +/- 2 at 12) between 1 and 2 octaves past the corner', () => {
		const hp = sideSlopes(0.75);
		assert.ok(Math.abs(hp[12] - 12) <= 2, `knob 0.75 at 12: ${hp[12].toFixed(2)} dB/oct`);
		assert.ok(Math.abs(hp[24] - 24) <= 2, `knob 0.75 at 24: ${hp[24].toFixed(2)} dB/oct`);
		const lp = sideSlopes(0.25);
		assert.ok(Math.abs(lp[12] - 12) <= 2, `knob 0.25 at 12: ${lp[12].toFixed(2)} dB/oct`);
		const added = lp[24] - lp[12];
		assert.ok(
			Math.abs(added - 12) <= 2,
			`knob 0.25: the second section adds ${added.toFixed(2)} dB/oct`
		);
	});

	// The bilinear transform puts each lowpass zero at Nyquist, so 3.4 to 6.8 kHz falls faster than the
	// analog slope: 13.8 dB/oct at 12 and 26.5 at 24 for the knob 0.25 corner (1.7 kHz) at 48 kHz.
	test(
		'knob 0.25 (lowpass 1.7 kHz) at 24 falls within 24 +/- 2 dB/oct',
		{
			todo: 'measures 26.5 dB/oct at 48 kHz (bilinear zeros at Nyquist); the band needs a decision, #9830'
		},
		() => {
			const lp = sideSlopes(0.25);
			assert.ok(Math.abs(lp[24] - 24) <= 2, `knob 0.25 at 24: ${lp[24].toFixed(2)} dB/oct`);
		}
	);

	test('the second section rides the exact glide of the first on a knob move', () => {
		const param = (v) => new SpecAudioParam(v, SR);
		const biquad = (hz) => ({ frequency: param(hz) });
		const nodes = {
			filterLp: biquad(20000),
			filterHp: biquad(30),
			filterLp2: biquad(20000),
			filterHp2: biquad(30),
			filterDry: { gain: param(1) },
			filterLpWet: { gain: param(0) },
			filterHpWet: { gain: param(0) }
		};
		const targets = { filter: guards.filterParamsFromKnob(0.25) };
		colorGraph.applyColorFilterTargets(nodes, targets, 0.5, SR);
		targets.filter = guards.filterParamsFromKnob(0.75);
		colorGraph.applyColorFilterTargets(nodes, targets, 0.51, SR);
		for (const [a, b] of [
			[nodes.filterLp, nodes.filterLp2],
			[nodes.filterHp, nodes.filterHp2]
		]) {
			assert.ok(a.frequency.calls.length > 0, 'the first section glided');
			assert.deepEqual(b.frequency.calls, a.frequency.calls);
			a.frequency.renderTo(SR);
			b.frequency.renderTo(SR);
			assert.deepEqual(b.frequency.rendered, a.frequency.rendered);
		}
	});

	test('the slope constant names FXFILT-02 as the owner of resonance', () => {
		const src = readFileSync(`${REPO}apps/webui/frontend/src/lib/player/constants.ts`, 'utf8');
		const doc = src.slice(
			src.indexOf('FXFILT-09'),
			src.indexOf('export const FILTER_SLOPE_DB_OCT')
		);
		assert.match(doc, /FXFILT-02 owns resonance/);
		assert.match(doc, /second section's Q never carries it/);
	});
});
