import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/next-only-filter.ts');
});

describe('computeNextOnlyRef', () => {
	it('uses the key a shifted reference deck is heard in (KEYSHIFT-03)', () => {
		const slice = { is_master: true, playing: true, stable_id: 'a', key: '3A', bpm: 128, pitch: 1, bpm_confidence: null };
		// 3A (Bbm) down one semitone sounds as Am, 8A.
		assert.deepEqual(mod.computeNextOnlyRef([{ ...slice, key_shift_semitones: -1 }]), {
			key: '8A',
			bpm: 128
		});
		// Controls: no shift keeps the source key verbatim. A missing shift
		// field is a caller bug and must throw, not read as zero (Sol P1 on #6574).
		assert.deepEqual(mod.computeNextOnlyRef([{ ...slice, key_shift_semitones: 0, pitch: 1, bpm_confidence: null }]), {
			key: '3A',
			bpm: 128
		});
		assert.throws(() => mod.computeNextOnlyRef([slice]), /has no key shift/);
	});

	it('prefers master deck key even when not playing', () => {
		const ref = mod.computeNextOnlyRef([
			{
				is_master: true,
				playing: false,
				stable_id: 'a',
				key_shift_semitones: 0, pitch: 1, bpm_confidence: null,
				key: '8A',
				bpm: 128
			},
			{
				is_master: false,
				playing: true,
				stable_id: 'b',
				key_shift_semitones: 0, pitch: 1, bpm_confidence: null,
				key: '9A',
				bpm: 130
			}
		]);
		assert.deepEqual(ref, { key: '8A', bpm: 128 });
	});

	it('falls through to playing deck when master lacks key', () => {
		const ref = mod.computeNextOnlyRef([
			{
				is_master: true,
				playing: false,
				stable_id: 'a',
				key_shift_semitones: 0, pitch: 1, bpm_confidence: null,
				key: null,
				bpm: 128
			},
			{
				is_master: false,
				playing: true,
				stable_id: 'b',
				key_shift_semitones: 0, pitch: 1, bpm_confidence: null,
				key: '9A',
				bpm: 130
			}
		]);
		assert.deepEqual(ref, { key: '9A', bpm: 130 });
	});

	it('updates when master reassignment changes key', () => {
		const before = mod.computeNextOnlyRef([
			{ is_master: true, playing: false, stable_id: '1', key: '8A', bpm: 124, key_shift_semitones: 0, pitch: 1, bpm_confidence: null },
			{ is_master: false, playing: false, stable_id: '2', key: '10A', bpm: 126, key_shift_semitones: 0, pitch: 1, bpm_confidence: null }
		]);
		const after = mod.computeNextOnlyRef([
			{ is_master: false, playing: false, stable_id: '1', key: '8A', bpm: 124, key_shift_semitones: 0, pitch: 1, bpm_confidence: null },
			{ is_master: true, playing: false, stable_id: '2', key: '10A', bpm: 126, key_shift_semitones: 0, pitch: 1, bpm_confidence: null }
		]);
		assert.notDeepEqual(before, after);
		assert.deepEqual(after, { key: '10A', bpm: 126 });
	});
});

describe('reference tempo (COMPATBPM-01, COMPATBPM-03)', () => {
	const deck = (o) => ({
		is_master: true, playing: true, stable_id: 'm', key: '8A', bpm: 128,
		pitch: 1, bpm_confidence: null, key_shift_semitones: 0, ...o
	});

	it('uses the heard tempo: a 128 master pitched +8% is a 138.24 reference', () => {
		const ref = mod.computeNextOnlyRef([deck({ pitch: 1.08 })]);
		assert.ok(Math.abs(ref.bpm - 138.24) < 1e-9, String(ref.bpm));
		assert.equal(mod.computeNextOnlyRef([deck({ pitch: 1 })]).bpm, 128, 'control: unpitched');
	});

	it('a missing tempo ratio throws instead of reading as unpitched', () => {
		assert.throws(() => mod.computeNextOnlyRef([deck({ pitch: undefined })]), /no tempo ratio/);
	});

	it('a placeholder-BPM master (confidence 0) is passed over for the next deck', () => {
		const ref = mod.computeNextOnlyRef([
			deck({ stable_id: 'placeholder', bpm: 125, bpm_confidence: 0 }),
			deck({ is_master: false, stable_id: 'real', key: '9A', bpm: 86, bpm_confidence: null })
		]);
		assert.deepEqual(ref, { key: '9A', bpm: 86 });
		assert.equal(mod.computeNextOnlyRef([deck({ bpm: 125, bpm_confidence: 0 })]), null, 'no other deck: no reference');
	});

	it('bpmConfidenceIn reads the loaded track from the held rows', () => {
		const rows = [{ stable_id: 'a', bpm_confidence: 0 }, { stable_id: 'b', bpm_confidence: null }];
		assert.equal(mod.bpmConfidenceIn('a', rows), 0);
		assert.equal(mod.bpmConfidenceIn('b', rows), null);
		assert.equal(mod.bpmConfidenceIn('missing', rows), null);
		assert.equal(mod.bpmConfidenceIn(null, rows), null);
		assert.equal(mod.bpmConfidenceIn('a', null), null);
	});
});
