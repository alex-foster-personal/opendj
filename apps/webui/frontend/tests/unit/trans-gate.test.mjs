// requirement: FXGATE-10418
// [if] TRANS_CUT_BEATS drifts from 1/16..16 with 3/4, or TRANS_TIME_MS from 10..16000, or an off-list value passes [then] broken
// [if] transGain is not 1 across the open hold from phase 0, 0 across the closed part, or its edgeFrac-0 integral is not the duty [then] broken
// [if] an edge is not a raised cosine (half gain at its midpoint, monotone) or two edges can exceed half the cycle [then] broken
// [if] TRANS_EDGE_SHAPES is not hard 1 ms, soft 4 ms, smooth 10 ms with soft as the default [then] broken
// [if] transMix gives wet above 0 at knob 0, or balance_then_duty does not reach duty/2 at knob 1 [then] broken
// [if] transCycleSeconds disagrees with the mod FX LFO's lfoCycleSeconds or accepts a tempo outside 40..300 [then] broken
// [if] the new files carry U+2014 or U+2013 [then] broken
// [if] TRANS refuses a triplet cut cycle or its gate does not repeat with the 1/3 or 4/3 beat period [then] broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let gate;
let lfo;

before(async () => {
	gate = await loadTypeScriptModule('src/lib/rb/trans-gate.ts');
	lfo = await loadTypeScriptModule('src/lib/rb/mod-fx-lfo.ts');
});

/** Midpoint-rule integral of transGain over one cycle. */
function integrateGain(duty, edgeFrac, samples) {
	let sum = 0;
	for (let i = 0; i < samples; i++) sum += gate.transGain((i + 0.5) / samples, duty, edgeFrac);
	return sum / samples;
}

const BPM_174_SIXTEENTH_SEC = 60 / 174 / 16;

describe('trans-gate constants', () => {
	test('cut beats and free time bounds match the A9 TRANS row plus 3/4', () => {
		assert.deepEqual(
			[...gate.TRANS_CUT_BEATS],
			[1 / 16, 1 / 8, 1 / 4, 1 / 2, 3 / 4, 1, 2, 4, 8, 16]
		);
		assert.deepEqual({ ...gate.TRANS_TIME_MS }, { min: 10, max: 16000 });
		assert.ok(Object.isFrozen(gate.TRANS_TIME_MS));
	});

	test('an off-list division or an out-of-range time throws naming the field', () => {
		assert.throws(() => gate.assertTransCutBeats(0.3), /beats must be one of .* got 0\.3/);
		assert.throws(() => gate.assertTransCutBeats(32), /beats/);
		assert.throws(() => gate.assertTransTimeMs(9), /timeMs .* got 9/);
		assert.throws(() => gate.assertTransTimeMs(16001), /timeMs .* got 16001/);
		assert.throws(() => gate.assertTransTimeMs(Number.NaN), /timeMs/);
		assert.doesNotThrow(() => gate.assertTransTimeMs(10));
		assert.doesNotThrow(() => gate.assertTransTimeMs(16000));
		assert.throws(() => gate.transCycleSeconds(0.3, 120), /beats/);
		assert.throws(() => gate.transTimeCycleSeconds(5), /timeMs/);
	});
});

describe('transGain envelope', () => {
	test('edgeFrac 0: 1 across the open hold from phase 0, 0 across the closed part', () => {
		for (const duty of [0.25, 0.5, 0.8]) {
			assert.equal(gate.transGain(0, duty, 0), 1, `downbeat open at duty ${duty}`);
			assert.equal(gate.transGain(duty / 2, duty, 0), 1);
			assert.equal(gate.transGain(duty - 1e-9, duty, 0), 1);
			assert.equal(gate.transGain(duty, duty, 0), 0);
			assert.equal(gate.transGain((1 + duty) / 2, duty, 0), 0);
			assert.equal(gate.transGain(1 - 1e-9, duty, 0), 0);
		}
	});

	test('edgeFrac 0: the integral over one cycle equals the duty within 1e-6', () => {
		assert.equal(integrateGain(0.5, 0, 1_000_000), 0.5, 'grid-aligned duty is exact');
		const measured = integrateGain(0.37, 0, 4_000_000);
		assert.ok(
			Math.abs(measured - 0.37) < 1e-6,
			`integral ${measured} must be within 1e-6 of duty 0.37`
		);
	});

	test('with edges: hold stays 1, edges are raised cosines in the closed part, each adds half its width', () => {
		const duty = 0.5;
		const edge = 0.1;
		assert.equal(gate.transGain(0, duty, edge), 1);
		assert.equal(gate.transGain(duty - 1e-9, duty, edge), 1, 'the hold is never eaten by an edge');
		assert.equal(gate.transGain(duty, duty, edge), 1, 'the fall starts at full gain');
		assert.ok(
			Math.abs(gate.transGain(duty + edge / 2, duty, edge) - 0.5) < 1e-12,
			'fall midpoint is half'
		);
		assert.ok(gate.transGain(duty + edge, duty, edge) < 1e-12, 'closed after the fall');
		assert.equal(gate.transGain(0.75, duty, edge), 0);
		assert.ok(gate.transGain(1 - edge, duty, edge) < 1e-12, 'the rise starts from 0');
		assert.ok(
			Math.abs(gate.transGain(1 - edge / 2, duty, edge) - 0.5) < 1e-12,
			'rise midpoint is half'
		);
		assert.ok(
			gate.transGain(1 - 1e-9, duty, edge) > 1 - 1e-6,
			'fully open again at the next downbeat'
		);
		let previous = 1;
		for (let i = 0; i <= 100; i++) {
			const g = gate.transGain(duty + (edge * i) / 100, duty, edge);
			assert.ok(g <= previous + 1e-12, 'the fall is monotone');
			previous = g;
		}
		previous = 0;
		for (let i = 0; i < 100; i++) {
			const g = gate.transGain(1 - edge + (edge * i) / 100, duty, edge);
			assert.ok(g >= previous - 1e-12, 'the rise is monotone');
			previous = g;
		}
		const measured = integrateGain(duty, edge, 1_000_000);
		assert.ok(
			Math.abs(measured - (duty + edge)) < 1e-6,
			`integral ${measured} must be duty + edge = ${duty + edge} within 1e-6`
		);
	});

	test('a closed part shorter than two edges shrinks each edge to half of it', () => {
		const duty = 0.9;
		assert.equal(gate.transGain(0.9 - 1e-9, duty, 0.25), 1);
		assert.ok(
			gate.transGain(0.95, duty, 0.25) < 1e-12,
			'fall ends and rise starts at the midpoint of the closed part'
		);
		assert.ok(Math.abs(gate.transGain(0.925, duty, 0.25) - 0.5) < 1e-12);
		assert.ok(Math.abs(gate.transGain(0.975, duty, 0.25) - 0.5) < 1e-12);
		assert.equal(gate.transGain(0.3, 1, 0.1), 1, 'duty 1 never closes');
	});

	test('out-of-range inputs throw naming the field', () => {
		assert.throws(() => gate.transGain(1, 0.5, 0), /phase .* got 1/);
		assert.throws(() => gate.transGain(-0.1, 0.5, 0), /phase/);
		assert.throws(() => gate.transGain(0.5, 1.1, 0), /duty .* got 1\.1/);
		assert.throws(() => gate.transGain(0.5, 0.5, 0.26), /edgeFrac .* got 0\.26/);
		assert.throws(() => gate.transGain(Number.NaN, 0.5, 0), /phase/);
	});
});

describe('edge shapes', () => {
	test('named shapes are hard 1 ms, soft 4 ms, smooth 10 ms and soft is the default', () => {
		assert.deepEqual({ ...gate.TRANS_EDGE_SHAPES }, { hard: 1, soft: 4, smooth: 10 });
		assert.equal(gate.TRANS_EDGE_DEFAULT, 'soft');
		assert.equal(gate.TRANS_EDGE_MAX_FRAC, 0.25);
	});

	test('edgeFracFor converts the edge time to a phase fraction', () => {
		assert.ok(Math.abs(gate.edgeFracFor('soft', 0.5) - 0.008) < 1e-12);
		assert.ok(Math.abs(gate.edgeFracFor('hard', 1) - 0.001) < 1e-12);
		assert.ok(Math.abs(gate.edgeFracFor('smooth', 2) - 0.005) < 1e-12);
	});

	test('at 1/16 beat and 174 BPM two edges never exceed half the cycle', () => {
		const cycleSec = gate.transCycleSeconds(1 / 16, 174);
		assert.ok(Math.abs(cycleSec - BPM_174_SIXTEENTH_SEC) < 1e-12);
		for (const shape of Object.keys(gate.TRANS_EDGE_SHAPES)) {
			const frac = gate.edgeFracFor(shape, cycleSec);
			assert.ok(
				2 * frac <= 0.5 + 1e-12,
				`${shape}: two edges ${2 * frac} must fit in half the cycle`
			);
		}
		assert.equal(
			gate.edgeFracFor('smooth', cycleSec),
			0.25,
			'10 ms in a 21.6 ms cycle clamps to a quarter'
		);
		assert.ok(
			gate.edgeFracFor('soft', cycleSec) < 0.25,
			'4 ms in a 21.6 ms cycle is under the clamp'
		);
	});

	test('an unknown shape or a bad cycle throws naming it', () => {
		assert.throws(
			() => gate.edgeFracFor('medium', 0.5),
			/shape must be one of hard, soft, smooth, got medium/
		);
		assert.throws(() => gate.edgeFracFor('soft', 0), /cycleSec/);
		assert.throws(() => gate.edgeFracFor('soft', Number.NaN), /cycleSec/);
	});
});

describe('depth laws', () => {
	test('laws are balance and balance_then_duty with balance as the default', () => {
		assert.deepEqual(Object.keys(gate.TRANS_DEPTH_LAWS).sort(), ['balance', 'balance_then_duty']);
		assert.equal(gate.TRANS_DEPTH_LAW_DEFAULT, 'balance');
	});

	test('knob 0 gives wet 0 under both laws', () => {
		for (const law of Object.keys(gate.TRANS_DEPTH_LAWS)) {
			assert.deepEqual(gate.transMix(0, 0.5, law), { wet: 0, duty: 0.5 }, law);
		}
	});

	test('balance: the knob is the wet balance and duty is unchanged', () => {
		assert.deepEqual(gate.transMix(0.3, 0.5, 'balance'), { wet: 0.3, duty: 0.5 });
		assert.deepEqual(gate.transMix(1, 0.25, 'balance'), { wet: 1, duty: 0.25 });
	});

	test('balance_then_duty: wet rises to 1 by knob 0.5, then duty falls to half by knob 1', () => {
		assert.deepEqual(gate.transMix(0.25, 0.5, 'balance_then_duty'), { wet: 0.5, duty: 0.5 });
		assert.deepEqual(gate.transMix(0.5, 0.5, 'balance_then_duty'), { wet: 1, duty: 0.5 });
		assert.deepEqual(gate.transMix(0.75, 0.5, 'balance_then_duty'), { wet: 1, duty: 0.375 });
		assert.deepEqual(gate.transMix(1, 0.5, 'balance_then_duty'), { wet: 1, duty: 0.25 });
		assert.deepEqual(gate.transMix(1, 0.8, 'balance_then_duty'), { wet: 1, duty: 0.4 });
	});

	test('a knob outside 0..1 or an unknown law throws naming it', () => {
		assert.throws(() => gate.transMix(1.1, 0.5, 'balance'), /knob .* got 1\.1/);
		assert.throws(() => gate.transMix(0.5, 0.5, 'loud'), /law must be one of .* got loud/);
	});
});

describe('one tempo rule shared with the mod FX LFO', () => {
	test('transCycleSeconds equals lfoCycleSeconds for every cut division', () => {
		for (const beats of gate.TRANS_CUT_BEATS) {
			for (const bpm of [40, 120, 174, 300]) {
				assert.equal(gate.transCycleSeconds(beats, bpm), lfo.lfoCycleSeconds(beats, bpm));
				assert.ok(Math.abs(gate.transCycleSeconds(beats, bpm) - (beats * 60) / bpm) < 1e-12);
			}
		}
	});

	test('a tempo outside 40..300 BPM returns null, like the LFO', () => {
		assert.equal(gate.transCycleSeconds(1, 39.9), null);
		assert.equal(gate.transCycleSeconds(1, 300.1), null);
		assert.equal(lfo.lfoCycleSeconds(1, 39.9), null);
		assert.throws(() => gate.transCycleSeconds(1, Number.NaN), /bpm/);
	});

	test('the free time path is the TIME in seconds within 10..16000 ms', () => {
		assert.equal(gate.transTimeCycleSeconds(500), 0.5);
		assert.equal(gate.transTimeCycleSeconds(16000), 16);
	});
});

describe('source hygiene', () => {
	test('the new files carry no U+2014 or U+2013', () => {
		const bannedDashes = new RegExp(`[${String.fromCharCode(0x2013, 0x2014)}]`);
		for (const relative of ['../../src/lib/rb/trans-gate.ts', './trans-gate.test.mjs']) {
			const text = readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8');
			assert.ok(!bannedDashes.test(text), `${relative} must not carry U+2013 or U+2014`);
		}
	});
});

// requirement: BEATFX-14715
describe('triplet cut cycles (issue #10016)', () => {
	test('transGain on a triplet LFO phase repeats every 1/3 and 4/3 beat', () => {
		// [if] transCycleSeconds refuses 1/3 or 4/3, or the gate on the LFO phase differs one cycle later [then] the triplet gate drifts, [else stop].
		assert.deepEqual([...gate.TRANS_TRIPLET_CUT_BEATS], [...lfo.TRIPLET_CYCLE_BEATS]);
		const grid = Array.from({ length: 400 }, (_unused, i) => ({
			n: (i % 4) + 1,
			bpm: 120,
			t: i * 0.5
		}));
		for (const beats of [1 / 3, 4 / 3]) {
			const cycleSec = gate.transCycleSeconds(beats, 120);
			assert.ok(Math.abs(cycleSec - beats * 0.5) < 1e-12);
			const anchor = lfo.lfoAnchor(grid, 4, 120, 0, beats);
			const edge = gate.edgeFracFor('soft', cycleSec);
			for (let t = 0.0013; t < 20; t += 0.0371) {
				const now = gate.transGain(lfo.lfoPhase(anchor, t), 0.5, edge);
				const next = gate.transGain(lfo.lfoPhase(anchor, t + cycleSec), 0.5, edge);
				assert.ok(Number.isFinite(now) && now >= 0 && now <= 1);
				assert.ok(Math.abs(now - next) < 1e-6, `${beats} beats at ${t}: ${now} vs ${next}`);
			}
			assert.equal(gate.transGain(lfo.lfoPhase(anchor, 0), 0.5, edge), 1, 'open on the downbeat');
		}
		assert.throws(() => gate.transCycleSeconds(3, 120), /beats/);
	});
});
