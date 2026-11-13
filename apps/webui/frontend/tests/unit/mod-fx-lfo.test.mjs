// requirement: FXMOD-9943
// [if] MOD_FX_CYCLE_BEATS or the MOD_FX_TIME_MS bounds drift from 1/16..64 beats and 10..32000 ms [then] broken
// [if] lfoAnchor gives a phase off the grid, with no downbeat or outside 40..300 BPM instead of null [then] broken
// [if] the phase is not 0 at every cycle-starting downbeat (cycles >= 1 beat) or on every beat (cycles < 1 beat) [then] broken
// [if] re-anchoring at a tempo step moves the phase by 1e-6 or more [then] broken
// [if] a free time_ms cycle does not repeat every time_ms or follows the grid [then] broken
// [if] NaN, a negative time or an unknown cycle is accepted silently [then] broken
// [if] a triplet cycle (1/12..12/3) is refused, or its trough misses a downbeat after every third cycle at 120 or 174 BPM [then] broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let lfo;

before(async () => {
	lfo = await loadTypeScriptModule('src/lib/rb/mod-fx-lfo.ts');
});

/** A 120 BPM grid (0.5 s per beat) from 0.25 s whose first beat is beat 3 of
 * its bar, so the first downbeat is grid index 2 at 1.25 s. */
const FIRST_DOWNBEAT_INDEX = 2;
function grid120(count = 600) {
	return Array.from({ length: count }, (_unused, i) => ({
		n: ((i + 2) % 4) + 1,
		bpm: 120,
		t: 0.25 + i * 0.5
	}));
}

/** Circular distance from phase 0, so 0.9999999999 reads as near 0. */
function fromZero(phase) {
	return Math.min(phase, 1 - phase);
}

describe('mod-fx-lfo constants', () => {
	test('cycle list and free time bounds match the DJM BEAT and TIME ranges', () => {
		assert.deepEqual(
			[...lfo.MOD_FX_CYCLE_BEATS],
			[1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32, 64]
		);
		assert.equal(lfo.MOD_FX_TIME_MS.min, 10);
		assert.equal(lfo.MOD_FX_TIME_MS.max, 32000);
	});
});

describe('lfoAnchor', () => {
	test('returns contextTime, beatIndex, beatsPerSec and the first downbeat as cycle origin', () => {
		const anchor = lfo.lfoAnchor(grid120(), 10.25, 126, 3, 4);
		assert.equal(anchor.contextTime, 3);
		assert.equal(anchor.beatIndex, 20);
		assert.equal(anchor.beatsPerSec, 126 / 60);
		assert.equal(anchor.cycleOriginBeat, FIRST_DOWNBEAT_INDEX);
		assert.equal(anchor.cycleBeats, 4);
	});

	test('returns null with no grid, no downbeat, off the grid or a tempo outside 40..300 BPM', () => {
		assert.equal(lfo.lfoAnchor([], 1, 120, 0, 1), null);
		const noDownbeat = grid120(40).map((beat) => ({ ...beat, n: 2 }));
		assert.equal(lfo.lfoAnchor(noDownbeat, 5, 120, 0, 1), null);
		assert.equal(lfo.lfoAnchor(grid120(), 0.1, 120, 0, 1), null, 'before the first beat');
		assert.equal(lfo.lfoAnchor(grid120(), 5, 39.9, 0, 1), null);
		assert.equal(lfo.lfoAnchor(grid120(), 5, 300.1, 0, 1), null);
		assert.notEqual(lfo.lfoAnchor(grid120(), 5, 40, 0, 1), null);
		assert.notEqual(lfo.lfoAnchor(grid120(), 5, 300, 0, 1), null);
	});
});

describe('lfoPhase on the grid', () => {
	test('phase is 0 within 1e-9 at every downbeat that starts a cycle, for cycles of 1 beat or more', () => {
		const beats = grid120();
		const positionSec = 7.3;
		const contextStart = 2.5;
		for (const cycleBeats of lfo.MOD_FX_CYCLE_BEATS.filter((c) => c >= 1)) {
			const anchor = lfo.lfoAnchor(beats, positionSec, 120, contextStart, cycleBeats);
			for (let k = 0; k * cycleBeats < 500; k += 1) {
				const beat = beats[FIRST_DOWNBEAT_INDEX + k * cycleBeats];
				if (!beat || beat.t < positionSec) continue;
				if (cycleBeats >= 4)
					assert.equal(beat.n, 1, 'a cycle of a bar or more starts on a downbeat');
				const phase = lfo.lfoPhase(anchor, contextStart + (beat.t - positionSec));
				assert.ok(fromZero(phase) < 1e-9, `cycle ${cycleBeats} k ${k}: phase ${phase}`);
			}
			// Half way through a cycle the phase is one half.
			const mid = beats[FIRST_DOWNBEAT_INDEX + 8 * cycleBeats].t + (cycleBeats * 0.5) / 2;
			if (mid > positionSec && cycleBeats <= 64) {
				assert.ok(Math.abs(lfo.lfoPhase(anchor, contextStart + (mid - positionSec)) - 0.5) < 1e-9);
			}
		}
	});

	test('phase is 0 on every beat for cycles under a beat', () => {
		const beats = grid120();
		for (const cycleBeats of lfo.MOD_FX_CYCLE_BEATS.filter((c) => c < 1)) {
			const anchor = lfo.lfoAnchor(beats, 3.1, 120, 0.4, cycleBeats);
			for (let i = 7; i < 120; i += 1) {
				const phase = lfo.lfoPhase(anchor, 0.4 + (beats[i].t - 3.1));
				assert.ok(fromZero(phase) < 1e-9, `cycle ${cycleBeats} beat ${i}: phase ${phase}`);
			}
		}
	});

	test('a tempo step keeps the phase continuous when re-anchored at the step moment', () => {
		const beats = grid120();
		const before = lfo.lfoAnchor(beats, 20.1, 120, 5, 2);
		const stepAt = 5 + 3.37; // 120 BPM on a 120 BPM grid: track time advances 1:1
		const phaseBefore = lfo.lfoPhase(before, stepAt);
		const after = lfo.lfoAnchor(beats, 20.1 + 3.37, 133, stepAt, 2);
		assert.ok(Math.abs(lfo.lfoPhase(after, stepAt) - phaseBefore) < 1e-6);
		// After the step the sweep runs at the new tempo: one beat later the phase moved by 1/2 cycle.
		const oneBeatLater = stepAt + 60 / 133;
		assert.ok(Math.abs(((lfo.lfoPhase(after, oneBeatLater) - phaseBefore + 1) % 1) - 0.5) < 1e-9);
	});
});

describe('lfoPhase free time_ms', () => {
	test('a free cycle ignores the grid and has period time_ms within 1e-9', () => {
		for (const timeMs of [10, 333, 1875, 32000]) {
			const anchor = lfo.lfoTimeAnchor(timeMs, 1.5);
			assert.equal(lfo.lfoPhase(anchor, 1.5), 0);
			for (let k = 1; k <= 5; k += 1) {
				assert.ok(
					fromZero(lfo.lfoPhase(anchor, 1.5 + (k * timeMs) / 1000)) < 1e-9,
					`${timeMs} ms cycle ${k}`
				);
			}
			assert.ok(Math.abs(lfo.lfoPhase(anchor, 1.5 + timeMs / 4000) - 0.25) < 1e-9);
			assert.equal('beatIndex' in anchor, false);
		}
	});
});

describe('invalid input throws by field name', () => {
	test('NaN, negative time and unknown cycle are refused, never defaulted', () => {
		const beats = grid120();
		assert.throws(() => lfo.lfoAnchor(beats, Number.NaN, 120, 0, 1), /positionSec/);
		assert.throws(() => lfo.lfoAnchor(beats, -1, 120, 0, 1), /positionSec/);
		assert.throws(() => lfo.lfoAnchor(beats, 5, 120, -0.1, 1), /contextTime/);
		assert.throws(() => lfo.lfoAnchor(beats, 5, Number.NaN, 0, 1), /bpm/);
		assert.throws(() => lfo.lfoAnchor(beats, 5, 120, 0, 3), /cycleBeats/);
		assert.throws(() => lfo.lfoTimeAnchor(9, 0), /timeMs/);
		assert.throws(() => lfo.lfoTimeAnchor(32001, 0), /timeMs/);
		assert.throws(() => lfo.lfoTimeAnchor(Number.NaN, 0), /timeMs/);
		const anchor = lfo.lfoAnchor(beats, 5, 120, 0, 1);
		assert.throws(() => lfo.lfoPhase(anchor, Number.NaN), /contextTime/);
		assert.throws(() => lfo.lfoPhase(anchor, -1), /contextTime/);
	});
});

test('no U+2014 or U+2013 characters in the new files', () => {
	const banned = new RegExp(`[${String.fromCharCode(0x2014)}${String.fromCharCode(0x2013)}]`);
	for (const rel of ['../../src/lib/rb/mod-fx-lfo.ts', './mod-fx-lfo.test.mjs']) {
		const text = readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
		assert.equal(banned.test(text), false, rel);
	}
});

/** A grid at `bpm` from 0.25 s whose first beat is beat 3 of its bar (first downbeat at index 2). */
function gridAt(bpm, count = 800) {
	return Array.from({ length: count }, (_unused, i) => ({
		n: ((i + 2) % 4) + 1,
		bpm,
		t: 0.25 + (i * 60) / bpm
	}));
}

// requirement: BEATFX-14715
describe('lfoPhase triplet cycles (issue #10016)', () => {
	test('triplet cycle list is 1/12..12/3 beats and lfoAnchor accepts every entry', () => {
		// [if] TRIPLET_CYCLE_BEATS drifts from the A9 1/12..12/3 set or lfoAnchor refuses one [then] TRIPLET FILTER cannot lock, [else stop].
		assert.deepEqual([...lfo.TRIPLET_CYCLE_BEATS], [1 / 12, 1 / 6, 1 / 3, 2 / 3, 4 / 3, 8 / 3, 4]);
		for (const cycleBeats of lfo.TRIPLET_CYCLE_BEATS) {
			assert.equal(lfo.lfoAnchor(grid120(), 7.3, 120, 0, cycleBeats).cycleBeats, cycleBeats);
		}
		assert.throws(() => lfo.lfoAnchor(grid120(), 7.3, 120, 0, 3), /cycleBeats/);
	});

	test('the trough lands on a bar downbeat after every third cycle at 120 and 174 BPM', () => {
		// [if] a triplet cycle of 1 beat or more is off phase 0 by 1e-9 at the downbeat ending each third cycle [then] the sweep drifts, [else stop].
		for (const bpm of [120, 174]) {
			const beats = gridAt(bpm);
			const positionSec = beats[9].t + 0.013;
			for (const cycleBeats of lfo.TRIPLET_CYCLE_BEATS.filter((c) => c >= 1)) {
				const anchor = lfo.lfoAnchor(beats, positionSec, bpm, 1.5, cycleBeats);
				const span = Math.round(3 * cycleBeats);
				let checked = 0;
				for (let i = FIRST_DOWNBEAT_INDEX + span; i < beats.length; i += span) {
					if (beats[i].t < positionSec) continue;
					assert.equal(beats[i].n, 1, `${bpm} BPM cycle ${cycleBeats}: beat ${i} is a downbeat`);
					const phase = lfo.lfoPhase(anchor, 1.5 + (beats[i].t - positionSec));
					assert.ok(fromZero(phase) < 1e-9, `${bpm} BPM cycle ${cycleBeats} beat ${i}: ${phase}`);
					checked += 1;
				}
				assert.ok(checked > 10);
				// One cycle after a trough the phase is back at 0; a third of a cycle later it is 1/3.
				const third = beats[FIRST_DOWNBEAT_INDEX + 4 * span].t + (cycleBeats * 60) / bpm / 3;
				assert.ok(Math.abs(lfo.lfoPhase(anchor, 1.5 + (third - positionSec)) - 1 / 3) < 1e-9);
			}
		}
	});
});
