import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/next-only-filter.ts');
});

describe('computeNextOnlyRef', () => {
	it('uses the key a shifted reference deck is heard in (KEYSHIFT-03)', () => {
		const slice = { is_master: true, playing: true, stable_id: 'a', key: '3A', bpm: 128 };
		// 3A (Bbm) down one semitone sounds as Am, 8A.
		assert.deepEqual(mod.computeNextOnlyRef([{ ...slice, key_shift_semitones: -1 }]), {
			key: '8A',
			bpm: 128
		});
		// Controls: no shift keeps the source key verbatim. A missing shift
		// field is a caller bug and must throw, not read as zero (Sol P1 on #6574).
		assert.deepEqual(mod.computeNextOnlyRef([{ ...slice, key_shift_semitones: 0 }]), {
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
				key_shift_semitones: 0,
				key: '8A',
				bpm: 128
			},
			{
				is_master: false,
				playing: true,
				stable_id: 'b',
				key_shift_semitones: 0,
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
				key_shift_semitones: 0,
				key: null,
				bpm: 128
			},
			{
				is_master: false,
				playing: true,
				stable_id: 'b',
				key_shift_semitones: 0,
				key: '9A',
				bpm: 130
			}
		]);
		assert.deepEqual(ref, { key: '9A', bpm: 130 });
	});

	it('updates when master reassignment changes key', () => {
		const before = mod.computeNextOnlyRef([
			{ is_master: true, playing: false, stable_id: '1', key: '8A', bpm: 124, key_shift_semitones: 0 },
			{ is_master: false, playing: false, stable_id: '2', key: '10A', bpm: 126, key_shift_semitones: 0 }
		]);
		const after = mod.computeNextOnlyRef([
			{ is_master: false, playing: false, stable_id: '1', key: '8A', bpm: 124, key_shift_semitones: 0 },
			{ is_master: true, playing: false, stable_id: '2', key: '10A', bpm: 126, key_shift_semitones: 0 }
		]);
		assert.notDeepEqual(before, after);
		assert.deepEqual(after, { key: '10A', bpm: 126 });
	});
});
