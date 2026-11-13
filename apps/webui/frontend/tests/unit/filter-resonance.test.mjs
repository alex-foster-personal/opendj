// requirement: FXFILT-14439
/**
 * Issue #9826 core slice: FILTER resonance knob to Q in dB, onset and edge guards, makeup. Verified offline, no listening.
 *
 * [if] the resonance knob maps 0 and 1 off the flat Q and the ceiling, or not strictly rising [then] the mapping test fails [else stop].
 * [if] resonance at FILTER_RES_DEFAULT gives any Q but FILTER_Q_DB or a makeup but 1 at any FILTER position [then] the default test fails [else stop].
 * [if] Q moves with COLOR travel between the 10% onset and the 15% edge [then] the never-linked test fails [else stop].
 * [if] Q is not flat at the detent edge or not a straight dB ramp over the first 10% of travel [then] the onset test fails [else stop].
 * [if] Q is not flat at full LP or HP travel or not a straight dB ramp over the last 15% [then] the edge test fails [else stop].
 * [if] max resonance moves RBJ pink-noise level at FILTER 0.25 or 0.75 by 1 dB or more [then] the makeup test fails [else stop].
 * [if] a cutoff sine at max resonance is not lifted, or is lifted past the uncompensated peak [then] the corner test fails [else stop].
 * [if] any finite knob pair gives a NaN, infinite or out-of-range Q or makeup [then] the finite test fails [else stop].
 * [if] a non-finite or out-of-range knob is not refused with a RangeError naming it [then] the refusal test fails [else stop].
 */
import assert from 'node:assert/strict';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SR = 48000;
const PINK_POINTS = 4000;

let m;

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/filter-resonance-entry.ts');
});

/** RBJ Audio EQ Cookbook LP/HP biquad magnitude at f, Q in dB as Web Audio takes it. */
function rbjMagnitude(side, cornerHz, qDb, f) {
	const w0 = (2 * Math.PI * cornerHz) / SR;
	const cos = Math.cos(w0);
	const alpha = Math.sin(w0) / (2 * 10 ** (qDb / 20));
	const b =
		side === 'lp'
			? [(1 - cos) / 2, 1 - cos, (1 - cos) / 2]
			: [(1 + cos) / 2, -(1 + cos), (1 + cos) / 2];
	const a = [1 + alpha, -2 * cos, 1 - alpha];
	const w = (2 * Math.PI * f) / SR;
	const abs = (c) =>
		Math.hypot(
			c[0] + c[1] * Math.cos(w) + c[2] * Math.cos(2 * w),
			c[1] * Math.sin(w) + c[2] * Math.sin(2 * w)
		);
	return abs(b) / abs(a);
}

/** Mean power of pink noise (equal power per octave, 20 Hz to 20 kHz) through one RBJ side. */
function pinkPower(side, cornerHz, qDb, gain) {
	let sum = 0;
	for (let i = 0; i < PINK_POINTS; i++) {
		const f = 20 * 1000 ** ((i + 0.5) / PINK_POINTS);
		sum += (gain * rbjMagnitude(side, cornerHz, qDb, f)) ** 2;
	}
	return sum / PINK_POINTS;
}

/** FILTER knob value at a travel 0..1 on one side, inverse of the dead-zone law. */
function knobAtTravel(side, travel) {
	const colour = 0.02 + travel * (1 - 0.02);
	return side === 'lp' ? 0.5 - colour / 2 : 0.5 + colour / 2;
}

function sideAndCorner(knob) {
	const law = m.filterParamsFromKnob(knob);
	return knob < 0.5 ? ['lp', law.lpHz] : ['hp', law.hpHz];
}

describe('filter resonance mapping', () => {
	test('[if] the knob runs 0..1 [then] Q runs FILTER_Q_DB dB to 20*log10(ceiling) dB, strictly rising, [else stop].', () => {
		assert.equal(m.resonanceQDbFromKnob(0), m.FILTER_Q_DB);
		assert.equal(m.FILTER_RES_Q_DB_FLAT, m.FILTER_Q_DB);
		assert.ok(
			Math.abs(m.resonanceQDbFromKnob(1) - 20 * Math.log10(m.FILTER_RES_Q_MAX_LINEAR)) < 1e-12
		);
		let previous = -Infinity;
		for (let i = 0; i <= 100; i++) {
			const qDb = m.resonanceQDbFromKnob(i / 100);
			assert.ok(qDb > previous, `not rising at knob ${i / 100}`);
			previous = qDb;
		}
	});

	test('[if] resonance sits at FILTER_RES_DEFAULT [then] Q is exactly FILTER_Q_DB and makeup exactly 1 at every FILTER position, [else stop].', () => {
		for (let i = 0; i <= 1000; i++) {
			assert.deepEqual(m.filterResonanceParams(i / 1000, m.FILTER_RES_DEFAULT), {
				qDb: m.FILTER_Q_DB,
				makeupGain: 1
			});
		}
	});
});

describe('filter resonance guards', () => {
	test('[if] COLOR travel lies in 0.10..0.85 on either side [then] Q equals the set Q exactly, [else stop].', () => {
		for (const side of ['lp', 'hp']) {
			for (const resonance of [0.25, 0.6, 1]) {
				for (let i = 0; i <= 75; i++) {
					const travel = 0.1 + i / 100;
					const { qDb } = m.filterResonanceParams(knobAtTravel(side, travel), resonance);
					assert.ok(
						Math.abs(qDb - m.resonanceQDbFromKnob(resonance)) < 1e-12,
						`${side} travel ${travel}`
					);
				}
			}
		}
	});

	test('[if] the knob just leaves the detent at max resonance [then] Q starts at flat and ramps in dB over 10% of travel, [else stop].', () => {
		const span = m.FILTER_RES_Q_DB_MAX - m.FILTER_Q_DB;
		for (const side of ['lp', 'hp']) {
			assert.equal(m.filterResonanceParams(side === 'lp' ? 0.495 : 0.505, 1).qDb, m.FILTER_Q_DB);
			const nearDetent = m.filterResonanceParams(knobAtTravel(side, 0.001), 1).qDb;
			assert.ok(
				nearDetent - m.FILTER_Q_DB < 0.011 * span,
				`${side} jumps ${nearDetent - m.FILTER_Q_DB} dB off the detent`
			);
			for (const travel of [0.025, 0.05, 0.075]) {
				const qDb = m.filterResonanceParams(knobAtTravel(side, travel), 1).qDb;
				assert.ok(
					Math.abs(qDb - (m.FILTER_Q_DB + (travel / 0.1) * span)) < 1e-9,
					`${side} travel ${travel}`
				);
			}
		}
		assert.equal(m.FILTER_Q_ONSET_FRAC, 0.1);
	});

	test('[if] the knob nears full LP or HP travel at max resonance [then] Q ramps back to flat in dB over 15%, flat at the end, [else stop].', () => {
		const span = m.FILTER_RES_Q_DB_MAX - m.FILTER_Q_DB;
		assert.deepEqual(m.filterResonanceParams(0, 1), { qDb: m.FILTER_Q_DB, makeupGain: 1 });
		assert.deepEqual(m.filterResonanceParams(1, 1), { qDb: m.FILTER_Q_DB, makeupGain: 1 });
		for (const side of ['lp', 'hp']) {
			for (const travel of [0.9, 0.95, 0.99]) {
				const qDb = m.filterResonanceParams(knobAtTravel(side, travel), 1).qDb;
				assert.ok(
					Math.abs(qDb - (m.FILTER_Q_DB + ((1 - travel) / 0.15) * span)) < 1e-9,
					`${side} travel ${travel}`
				);
			}
		}
		assert.equal(m.FILTER_Q_EDGE_FRAC, 0.15);
	});
});

describe('filter resonance makeup', () => {
	test('[if] resonance is max at FILTER 0.25 (LP) and 0.75 (HP) [then] RBJ pink-noise level is within 1 dB of default, [else stop].', () => {
		for (const knob of [0.25, 0.75]) {
			const [side, corner] = sideAndCorner(knob);
			const res = m.filterResonanceParams(knob, 1);
			const flat = m.filterResonanceParams(knob, m.FILTER_RES_DEFAULT);
			const unmatchedDb =
				10 * Math.log10(pinkPower(side, corner, res.qDb, 1) / pinkPower(side, corner, flat.qDb, 1));
			const matchedDb =
				10 *
				Math.log10(
					pinkPower(side, corner, res.qDb, res.makeupGain) / pinkPower(side, corner, flat.qDb, 1)
				);
			assert.ok(
				unmatchedDb > 1,
				`${side} needs no makeup (${unmatchedDb} dB), test is not discriminating`
			);
			assert.ok(Math.abs(matchedDb) < 1, `${side} at ${knob} moves ${matchedDb} dB`);
		}
	});

	test('[if] a sine sits at the cutoff at max resonance [then] it is lifted above default but never past the raw RBJ peak, [else stop].', () => {
		for (let i = 1; i < 100; i++) {
			const knob = i / 100;
			const res = m.filterResonanceParams(knob, 1);
			if (res.qDb - m.FILTER_Q_DB < 0.01) continue;
			const [side, corner] = sideAndCorner(knob);
			const flatAtCorner = rbjMagnitude(side, corner, m.FILTER_Q_DB, corner);
			const rawAtCorner = rbjMagnitude(side, corner, res.qDb, corner);
			const madeUp = res.makeupGain * rawAtCorner;
			assert.ok(
				madeUp > flatAtCorner && madeUp <= rawAtCorner,
				`${side} at ${knob}: ${madeUp} vs ${flatAtCorner}..${rawAtCorner}`
			);
			assert.ok(madeUp <= m.FILTER_RES_Q_MAX_LINEAR, `${side} at ${knob} exceeds the ceiling peak`);
		}
	});
});

describe('filter resonance safety', () => {
	test('[if] both knobs sweep 0..1 in fine steps [then] Q stays in flat..ceiling and makeup in (0, 1], all finite, [else stop].', () => {
		for (let i = 0; i <= 400; i++) {
			for (let j = 0; j <= 40; j++) {
				const { qDb, makeupGain } = m.filterResonanceParams(i / 400, j / 40);
				assert.ok(Number.isFinite(qDb) && Number.isFinite(makeupGain), `${i / 400}, ${j / 40}`);
				assert.ok(qDb >= m.FILTER_Q_DB && qDb <= m.FILTER_RES_Q_DB_MAX + 1e-12);
				assert.ok(makeupGain > 0 && makeupGain <= 1);
				assert.ok(makeupGain >= 2.2250738585072014e-308, 'subnormal makeup');
			}
		}
	});

	test('[if] a knob is NaN, infinite, below 0 or above 1 [then] it is refused with a RangeError naming that knob, [else stop].', () => {
		for (const bad of [Number.NaN, Infinity, -Infinity, -0.01, 1.01]) {
			assert.throws(
				() => m.resonanceQDbFromKnob(bad),
				(e) => e instanceof RangeError && /filter_resonance/.test(e.message)
			);
			assert.throws(
				() => m.filterResonanceParams(0.3, bad),
				(e) => e instanceof RangeError && /filter_resonance/.test(e.message)
			);
			assert.throws(
				() => m.filterResonanceParams(bad, 0.5),
				(e) => e instanceof RangeError && /^filter must/.test(e.message)
			);
		}
	});
});
