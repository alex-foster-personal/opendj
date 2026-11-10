// SETPREP-11947: the compatible filter's energy rule (within one, boost +1 to
// +2, drop -1 to -2), after Mixed In Key's Energy Boost convention (mimicked).
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let nof;
let copy;
let setting;

before(async () => {
	nof = await loadTypeScriptModule('src/lib/rb/next-only-filter.ts');
	copy = await loadTypeScriptModule('src/lib/rb/compatible-filter-copy.ts');
	setting = await loadTypeScriptModule('src/lib/settings/compatible-filter-setting.ts');
});

const prefsWith = (energy_mode) => ({ ...nof.COMPATIBLE_FILTER_DEFAULTS, energy_mode });
const REF = { key: '8A', bpm: 124, energy: 5 };
const row = (energy) => ({ key: '8A', bpm: 124, energy });
const kept = (mode, ref = REF) =>
	[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].filter((e) =>
		nof.isAppropriateNext(row(e), ref, prefsWith(mode))
	);

test('within_1 keeps abs(row - ref) <= 1', () => {
	assert.deepEqual(kept('within_1'), [4, 5, 6]);
});

test('boost keeps ref + 1 and ref + 2', () => {
	assert.deepEqual(kept('boost'), [6, 7]);
});

test('drop keeps ref - 1 and ref - 2', () => {
	assert.deepEqual(kept('drop'), [3, 4]);
});

test('a row with no energy is excluded under every mode, and the note counts it', () => {
	for (const mode of ['within_1', 'boost', 'drop']) {
		assert.equal(nof.isAppropriateNext(row(null), REF, prefsWith(mode)), false);
		assert.equal(nof.isAppropriateNext({ key: '8A', bpm: 124 }, REF, prefsWith(mode)), false);
	}
	const p = prefsWith('boost');
	assert.equal(
		nof.compatibleEnergyNote([{ energy: null }, { energy: null }, { energy: 6 }], REF, p),
		'2 rows have no energy'
	);
	assert.equal(
		nof.compatibleEnergyNote([{ energy: null }, { energy: 6 }], REF, p),
		'1 row has no energy'
	);
	assert.equal(nof.compatibleEnergyNote([{ energy: 6 }], REF, p), null);
});

test('with no reference energy the rule is skipped and key and BPM still apply', () => {
	for (const ref of [
		{ key: '8A', bpm: 124, energy: null },
		{ key: '8A', bpm: 124 }
	]) {
		const p = prefsWith('boost');
		assert.equal(nof.isAppropriateNext(row(1), ref, p), true);
		assert.equal(nof.isAppropriateNext(row(null), ref, p), true);
		assert.equal(nof.isAppropriateNext({ key: '2B', bpm: 124, energy: 6 }, ref, p), false);
		assert.equal(nof.isAppropriateNext({ key: '8A', bpm: 170, energy: 6 }, ref, p), false);
		assert.equal(
			nof.compatibleEnergyNote([row(null)], ref, p),
			'Energy rule skipped: the reference track has no energy'
		);
	}
});

test('off or a missing energy_mode leaves the filter unchanged', () => {
	const { energy_mode: _e, ...missing } = nof.COMPATIBLE_FILTER_DEFAULTS;
	const rows = [
		row(1),
		row(null),
		{ key: '2B', bpm: 124, energy: 5 },
		{ key: '8A', bpm: 170, energy: 5 },
		{ key: '9A', bpm: 120 }
	];
	const expected = [true, true, false, false, true];
	for (const p of [prefsWith('off'), missing]) {
		assert.deepEqual(
			rows.map((r) => nof.isAppropriateNext(r, REF, p)),
			expected
		);
		assert.equal(nof.compatibleEnergyNote(rows, REF, p), null);
	}
	assert.equal(nof.compatibleEnergyNote(rows, null, prefsWith('boost')), null);
});

test('the summary names the energy rule only when it is on', () => {
	const off = copy.compatibleFilterSummary(prefsWith('off'));
	assert.ok(!off.includes('energy'));
	const { energy_mode: _e, ...missing } = nof.COMPATIBLE_FILTER_DEFAULTS;
	assert.equal(copy.compatibleFilterSummary(missing), off);
	assert.equal(copy.compatibleFilterSummary(prefsWith('boost')), `${off}, energy boost +1 to +2`);
	assert.equal(copy.compatibleFilterSummary(prefsWith('within_1')), `${off}, energy within 1`);
	assert.equal(copy.compatibleFilterSummary(prefsWith('drop')), `${off}, energy drop -1 to -2`);
});

const deck = (o) => ({
	is_master: false,
	playing: false,
	stable_id: null,
	key: '8A',
	bpm: 124,
	pitch: 1,
	bpm_confidence: null,
	key_shift_semitones: 0,
	...o
});

test('computeNextOnlyRef still returns exactly key and bpm; nextOnlyReferenceDeck picks the same deck', () => {
	const decks = [
		deck({ stable_id: 'p', playing: true, key: '3B', bpm: 100 }),
		deck({ stable_id: 'm', is_master: true })
	];
	assert.deepEqual(nof.computeNextOnlyRef(decks), { key: '8A', bpm: 124 });
	assert.equal(nof.nextOnlyReferenceDeck(decks)?.stable_id, 'm');
	assert.equal(nof.nextOnlyReferenceDeck([deck({})]), null);
});

test('energyIn reads the energy by stable_id', () => {
	const rows = [
		{ stable_id: 'a', energy: 7 },
		{ stable_id: 'b', energy: null }
	];
	assert.equal(nof.energyIn('a', rows), 7);
	assert.equal(nof.energyIn('b', rows), null);
	assert.equal(nof.energyIn('z', rows), null);
	assert.equal(nof.energyIn(null, rows), null);
});

test('the Settings row maps energy_mode and rejects an unknown value', () => {
	assert.deepEqual(setting.compatibleFilterSettingPatch('compatible_filter.energy_mode', 'drop'), {
		energy_mode: 'drop'
	});
	assert.throws(
		() => setting.compatibleFilterSettingPatch('compatible_filter.energy_mode', 'loud'),
		/energy_mode/
	);
});
