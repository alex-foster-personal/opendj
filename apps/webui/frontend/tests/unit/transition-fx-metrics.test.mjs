// requirement: FXTRANS-14643
// [if] tailIntrusionDb misplaces its first beat or late tail window, or is not K-weighted per BS.1770-4 [then] broken
// [if] landingErrorMs reads a kick placed 30 ms late outside 30 +- 2 ms [then] broken
// [if] a dry outgoing that stops at T_in reads TIR at or above -90 dB, or an equal level outgoing outside 0 +- 0.5 dB [then] broken
// [if] splitByLandingGate keeps a clip over 10 ms, drops one within it, or does not list the dropped ids [then] broken
// Fixtures are synthesized here (a click-train kick at 124 BPM and a pink-noise pad), offline, no audio committed.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import * as metrics from '../../src/lib/rb/transition-fx-metrics.ts';

const SR = 48000;
const BEAT_SEC = 60 / 124;
const BAR_SEC = 4 * BEAT_SEC;
const T_IN_SEC = 1;
const LENGTH = Math.round((T_IN_SEC + 3 * BAR_SEC) * SR);

/** Xorshift white noise in [-1, 1), deterministic per seed. */
function white(seed) {
	let state = seed >>> 0 || 1;
	return () => {
		state ^= state << 13;
		state ^= state >>> 17;
		state ^= state << 5;
		return (state >>> 0) / 0x80000000 - 1;
	};
}

/** Pink noise pad (Paul Kellet's refined filter) at a given peak-ish gain from startSec on. */
function pinkPad(seed, gain, startSec = 0) {
	const next = white(seed);
	const out = new Float64Array(LENGTH);
	let b0 = 0,
		b1 = 0,
		b2 = 0,
		b3 = 0,
		b4 = 0,
		b5 = 0,
		b6 = 0;
	for (let i = Math.round(startSec * SR); i < LENGTH; i++) {
		const w = next();
		b0 = 0.99886 * b0 + w * 0.0555179;
		b1 = 0.99332 * b1 + w * 0.0750759;
		b2 = 0.969 * b2 + w * 0.153852;
		b3 = 0.8665 * b3 + w * 0.3104856;
		b4 = 0.55 * b4 + w * 0.5329522;
		b5 = -0.7616 * b5 - w * 0.016898;
		out[i] = gain * 0.11 * (b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * 0.5362);
		b6 = w * 0.115926;
	}
	return out;
}

/** Click-train kick: a 1 ms click plus a 120 to 50 Hz sine body, one per beat from firstSec on. */
function kickTrain(firstSec) {
	const out = new Float64Array(LENGTH);
	for (let start = firstSec; start * SR < LENGTH; start += BEAT_SEC) {
		const s0 = Math.round(start * SR);
		let phase = 0;
		for (let i = 0; i < Math.round(0.25 * SR) && s0 + i < LENGTH; i++) {
			const t = i / SR;
			const hz = 50 + 70 * Math.exp(-t / 0.03);
			out[s0 + i] += 0.8 * Math.exp(-t / 0.08) * Math.sin(phase) + (i < SR / 1000 ? 0.3 : 0);
			phase += (2 * Math.PI * hz) / SR;
		}
	}
	return out;
}

const render = (...channels) => ({ sampleRate: SR, channels });
const sum = (a, b) => a.map((x, i) => x + b[i]);

describe('tailIntrusionDb', () => {
	test('[if] outgoing is 0.5x then 0.1x the incoming in the two windows [then] TIR reads -6.02 and -20 dB, K-weighted per BS.1770-4, [else stop].', () => {
		const incoming = sum(kickTrain(T_IN_SEC), pinkPad(7, 0.3, T_IN_SEC));
		const at = (sec) => Math.round(sec * SR); // window edges round to the nearest sample
		const gainAt = (i) => {
			if (i >= at(T_IN_SEC) && i < at(T_IN_SEC + BEAT_SEC)) return 0.5;
			if (i >= at(T_IN_SEC + BAR_SEC) && i < at(T_IN_SEC + 2 * BAR_SEC)) return 0.1;
			return 3; // loud outside both windows, which must not count
		};
		const outgoing = incoming.map((x, i) => x * gainAt(i));
		const tir = metrics.tailIntrusionDb(render(outgoing), render(incoming), T_IN_SEC, BEAT_SEC);
		assert.ok(
			Math.abs(tir.firstBeatDb - 20 * Math.log10(0.5)) < 1e-9,
			`first beat ${tir.firstBeatDb}`
		);
		assert.ok(Math.abs(tir.lateTailDb + 20) < 1e-9, `late tail ${tir.lateTailDb}`);
		// BS.1770-4 Annex 1 published 48 kHz coefficients.
		const [shelf, highPass] = metrics.kWeightingBiquads(48000);
		const expectShelf = {
			b0: 1.53512485958697,
			b1: -2.69169618940638,
			b2: 1.19839281085285,
			a1: -1.69065929318241,
			a2: 0.73248077421585
		};
		const expectHp = { b0: 1, b1: -2, b2: 1, a1: -1.99004745483398, a2: 0.99007225036621 };
		for (const key of Object.keys(expectShelf)) {
			assert.ok(Math.abs(shelf[key] - expectShelf[key]) < 1e-8, `shelf ${key} ${shelf[key]}`);
			assert.ok(
				Math.abs(highPass[key] - expectHp[key]) < 1e-8,
				`high-pass ${key} ${highPass[key]}`
			);
		}
		// K-weighted, not flat: a 30 Hz outgoing reads well under a 1 kHz incoming of the same RMS.
		const tone = (hz) =>
			Float64Array.from({ length: LENGTH }, (_, i) => Math.sin((2 * Math.PI * hz * i) / SR));
		const lowVsMid = metrics.tailIntrusionDb(
			render(tone(30)),
			render(tone(1000)),
			T_IN_SEC,
			BEAT_SEC
		);
		assert.ok(lowVsMid.firstBeatDb < -5, `30 Hz vs 1 kHz ${lowVsMid.firstBeatDb}`);
	});

	test('[if] a dry outgoing stops at T_in, or plays at equal level [then] TIR is below -90 dB, or 0 +- 0.5 dB, [else stop].', () => {
		const incoming = sum(kickTrain(T_IN_SEC), pinkPad(11, 0.3, T_IN_SEC));
		const stopped = pinkPad(23, 0.3).map((x, i) => (i < T_IN_SEC * SR ? x : 0));
		const negative = metrics.tailIntrusionDb(
			render(stopped, stopped),
			render(incoming, incoming),
			T_IN_SEC,
			BEAT_SEC
		);
		assert.ok(negative.firstBeatDb < -90 && negative.lateTailDb < -90, JSON.stringify(negative));
		const equal = sum(kickTrain(T_IN_SEC - 2 * BAR_SEC), pinkPad(29, 0.3));
		const positive = metrics.tailIntrusionDb(render(equal), render(incoming), T_IN_SEC, BEAT_SEC);
		assert.ok(Math.abs(positive.firstBeatDb) <= 0.5, `first beat ${positive.firstBeatDb}`);
		assert.ok(Math.abs(positive.lateTailDb) <= 0.5, `late tail ${positive.lateTailDb}`);
	});
});

describe('landingErrorMs', () => {
	test('[if] the incoming first kick lands 30 ms late over a pink pad [then] landing error reads 30 +- 2 ms, [else stop].', () => {
		const late = render(sum(kickTrain(T_IN_SEC + 0.03), pinkPad(3, 0.3, T_IN_SEC)));
		const error = metrics.landingErrorMs(late, T_IN_SEC);
		assert.ok(Math.abs(error - 30) <= 2, `late kick read ${error} ms`);
		const onTime = metrics.landingErrorMs(render(kickTrain(T_IN_SEC)), T_IN_SEC);
		assert.ok(Math.abs(onTime) <= 2, `on-time kick read ${onTime} ms`);
		assert.throws(
			() => metrics.landingErrorMs(render(new Float64Array(LENGTH)), T_IN_SEC),
			/no 40\.\.150 Hz energy/
		);
	});
});

describe('splitByLandingGate', () => {
	test('[if] clips land at 9.9, -10, 10.5 and -12 ms [then] the last two are dropped and listed by id, [else stop].', () => {
		const clips = [
			{ id: 'echo-out-30', landingErrorMs: 9.9 },
			{ id: 'brake-p2', landingErrorMs: -10 },
			{ id: 'backspin-2x', landingErrorMs: 10.5 },
			{ id: 'echo-out-6', landingErrorMs: -12 }
		];
		const { kept, droppedIds } = metrics.splitByLandingGate(clips);
		assert.deepEqual(
			kept.map((c) => c.id),
			['echo-out-30', 'brake-p2']
		);
		assert.deepEqual(droppedIds, ['backspin-2x', 'echo-out-6']);
		assert.equal(metrics.LANDING_GATE_MS, 10);
		assert.throws(
			() => metrics.splitByLandingGate([{ id: 'x', landingErrorMs: Number.NaN }]),
			/non-finite/
		);
	});
});
