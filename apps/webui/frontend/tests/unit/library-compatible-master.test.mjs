import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/next-only-filter.ts');
});

describe('computeNextOnlyRef', () => {
	it('prefers master deck key even when not playing', () => {
		const ref = mod.computeNextOnlyRef([
			{
				is_master: true,
				playing: false,
				stable_id: 'a',
				key: '8A',
				bpm: 128
			},
			{
				is_master: false,
				playing: true,
				stable_id: 'b',
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
				key: null,
				bpm: 128
			},
			{
				is_master: false,
				playing: true,
				stable_id: 'b',
				key: '9A',
				bpm: 130
			}
		]);
		assert.deepEqual(ref, { key: '9A', bpm: 130 });
	});

	it('updates when master reassignment changes key', () => {
		const before = mod.computeNextOnlyRef([
			{ is_master: true, playing: false, stable_id: '1', key: '8A', bpm: 124 },
			{ is_master: false, playing: false, stable_id: '2', key: '10A', bpm: 126 }
		]);
		const after = mod.computeNextOnlyRef([
			{ is_master: false, playing: false, stable_id: '1', key: '8A', bpm: 124 },
			{ is_master: true, playing: false, stable_id: '2', key: '10A', bpm: 126 }
		]);
		assert.notDeepEqual(before, after);
		assert.deepEqual(after, { key: '10A', bpm: 126 });
	});
});
