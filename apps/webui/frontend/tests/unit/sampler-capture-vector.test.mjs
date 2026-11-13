/**
 * Issue #11127 (S13 capture): a deck's mix state maps to the SamplerVector the capture stores,
 * through the channel strip's own laws (player/constants.ts), so the capture replays the deck.
 *
 * Regression lines:
 * - if EQ knobs are low 0.25 / mid 0.5 / high 1 then eq_db is -13 / 0 / 6
 * - if the filter knob is 0.25 then filter is -0.5, and the knob round-trips
 * - if stems are not ready then stems is null; if ready then each part keeps gain, mute and solo
 * - if a loop has no beat length then its beats come from the native BPM
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let V;
before(async () => {
	V = await loadTypeScriptModule('src/lib/player/sampler/sampler-capture-vector.ts');
});

const knob = (gain, o = {}) => ({ muted: false, solo: false, gain, ...o });
const deck = (o = {}) => ({
	trimKnob: 0.5,
	eqKnobs: { low: 0.25, mid: 0.5, high: 1 },
	filterKnob: 0.25,
	stems: null,
	keyShiftSt: -2,
	masterTempo: true,
	...o
});

test('if trim is at unity then trim_db is 0', () => {
	/** [if] the trim knob sits at 0.5 (unity gain) [then] trim_db is exactly 0, [else stop]. */
	assert.equal(V.vectorFromDeckMix(deck()).trim_db, 0);
});

test('if EQ knobs are low 0.25 / mid 0.5 / high 1 then eq_db is -13 / 0 / 6', () => {
	/** [if] EQ knobs are 0.25, 0.5 and 1 [then] eq_db is -13, 0 and 6 through eqDbFromKnob, [else stop]. */
	assert.deepEqual(V.vectorFromDeckMix(deck()).eq_db, { low: -13, mid: 0, high: 6 });
});

test('if the filter knob is 0.25 then filter is -0.5 (low-pass side)', () => {
	/** [if] the filter knob is 0.25 [then] the vector filter is -0.5, the low-pass side, [else stop]. */
	assert.equal(V.vectorFromDeckMix(deck()).filter, -0.5);
});

test('if the filter round-trips then the knob is unchanged', () => {
	/** [if] a knob goes to bipolar and back [then] it is unchanged, and an out-of-range filter throws, [else stop]. */
	assert.equal(V.filterKnobFromBipolar(V.filterBipolarFromKnob(0.8)), 0.8);
	assert.throws(() => V.filterKnobFromBipolar(1.5), /filter must be -1..1/);
});

test('if key shift is -2 with master tempo on then both are kept exactly', () => {
	/** [if] the deck is shifted -2 st with Master Tempo on [then] the vector keeps -2 and true, [else stop]. */
	const v = V.vectorFromDeckMix(deck());
	assert.deepEqual([v.key_shift_st, v.master_tempo], [-2, true]);
});

test('if stems are off then stems is null', () => {
	/** [if] the deck's stems are not ready [then] the vector's stems is null, [else stop]. */
	assert.equal(V.vectorFromDeckMix(deck()).stems, null);
});

test('if stems are on then gains, mutes and solo map per part with inst from instrumental', () => {
	/** [if] stems are ready [then] each part keeps gain dB, mute and solo, a silent gain is the floor, [else stop]. */
	const controls = {
		vocal: knob(0, { muted: true }),
		instrumental: knob(0.5, { solo: true }),
		drums: knob(0.5),
		bass: knob(1),
		other: knob(0.25)
	};
	const s = V.vectorFromDeckMix(deck({ stems: { controls, hasInstrumental: true } })).stems;
	assert.deepEqual(s, {
		vocals: { gain_db: V.VECTOR_FLOOR_DB, mute: true, solo: false },
		drums: { gain_db: 0, mute: false, solo: false },
		bass: { gain_db: 6, mute: false, solo: false },
		inst: { gain_db: 0, mute: false, solo: true }
	});
});

test('if the deck has no instrumental stem then inst comes from other', () => {
	/** [if] hasInstrumental is false [then] the vector's inst part is the deck's other stem, [else stop]. */
	const controls = {
		vocal: knob(0.5),
		instrumental: knob(1, { solo: true }),
		drums: knob(0.5),
		bass: knob(0.5),
		other: knob(0.5, { muted: true })
	};
	const s = V.vectorFromDeckMix(deck({ stems: { controls, hasInstrumental: false } })).stems;
	assert.deepEqual(s.inst, { gain_db: 0, mute: true, solo: false });
});

test('if a negative gain reaches linearToDb then it throws', () => {
	/** [if] linearToDb gets a negative or NaN gain [then] it throws instead of storing a bogus dB, [else stop]. */
	assert.throws(() => V.linearToDb(-0.1), /gain must be >= 0/);
	assert.throws(() => V.linearToDb(Number.NaN), /gain must be >= 0/);
});

test('if a loop has no beat length then beats come from the native BPM', () => {
	/** [if] a 3871 ms loop has no beat length on a 124 BPM deck [then] it counts 8 beats, [else stop]. */
	assert.equal(V.loopBeatCount(0, 3871, null, 124), 8);
});

test('if a loop has a beat length then it wins, and a loop with neither throws', () => {
	/** [if] the deck gives a beat length [then] it is used; with no length and no BPM it throws, [else stop]. */
	assert.equal(V.loopBeatCount(0, 3871, 16, 124), 16);
	assert.throws(() => V.loopBeatCount(0, 3871, null, null), /no beat length/);
	assert.throws(() => V.loopBeatCount(10, 5, 4, 124), /must be after in/);
});
