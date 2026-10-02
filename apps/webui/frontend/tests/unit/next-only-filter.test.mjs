import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/next-only-filter.ts');
});

describe('next-only-filter', () => {
	it('reveals one or two search rows only when an active filter hid every match', () => {
		const { resolveSearchFilterFallback, SEARCH_FILTER_FALLBACK_MAX_ROWS } = mod;
		const unfiltered = [{ stable_id: 'azara-1' }, { stable_id: 'azara-2' }];

		assert.equal(SEARCH_FILTER_FALLBACK_MAX_ROWS, 2);
		assert.deepEqual(
			resolveSearchFilterFallback([], unfiltered, ['next-only']),
			{ rows: unfiltered, ignoredFilters: ['next-only'] }
		);
		assert.deepEqual(
			resolveSearchFilterFallback([{ stable_id: 'kept' }], unfiltered, ['next-only']),
			{ rows: [{ stable_id: 'kept' }], ignoredFilters: [] }
		);
		assert.deepEqual(
			resolveSearchFilterFallback([], [...unfiltered, { stable_id: 'azara-3' }], ['next-only']),
			{ rows: [], ignoredFilters: [] }
		);
		assert.deepEqual(
			resolveSearchFilterFallback([], unfiltered, []),
			{ rows: [], ignoredFilters: [] }
		);
	});

	it('recovers only one or two hidden matches for a non-empty search', () => {
		const one = ['one'];
		const two = ['one', 'two'];
		assert.equal(mod.selectSearchFilterFallback('flare', [], one, true), one);
		assert.equal(mod.selectSearchFilterFallback('flare', [], two, true), two);
		assert.equal(mod.selectSearchFilterFallback('flare', [], [], true), null);
		assert.equal(mod.selectSearchFilterFallback('flare', [], ['one', 'two', 'three'], true), null);
		assert.equal(mod.selectSearchFilterFallback('  ', [], two, true), null);
		assert.equal(mod.selectSearchFilterFallback('flare', ['existing'], one, true), null);
	});

	it('honors an exact absolute BPM window', () => {
		const { bpmMatchesCompatiblePrefs, COMPATIBLE_FILTER_DEFAULTS } = mod;
		const prefs = { ...COMPATIBLE_FILTER_DEFAULTS, bpm_window_bpm: 6, allow_half_double: false };
		assert.equal(bpmMatchesCompatiblePrefs(100, 100, prefs), true);
		assert.equal(bpmMatchesCompatiblePrefs(106, 100, prefs), true);
		assert.equal(bpmMatchesCompatiblePrefs(106.1, 100, prefs), false);
		assert.equal(bpmMatchesCompatiblePrefs(null, 100, prefs), false);
		assert.equal(bpmMatchesCompatiblePrefs(100, null, prefs), false);
		assert.equal(bpmMatchesCompatiblePrefs(0, 100, prefs), false);
	});

	it('never recovers incomplete or stale search results', () => {
		const rows = ['one'];
		assert.equal(mod.selectSearchFilterFallback('flare', [], rows, false), null);
		assert.equal(mod.selectSearchFilterFallback('flare', [], rows, true), rows);
	});

	it('keeps half/double within 15 BPM as appropriate', () => {
		const { bpmMatchesCompatiblePrefs, isAppropriateNext, COMPATIBLE_FILTER_DEFAULTS } = mod;
		const halfDoublePrefs = { ...COMPATIBLE_FILTER_DEFAULTS, bpm_window_bpm: 6 };
		assert.equal(bpmMatchesCompatiblePrefs(90, 180, halfDoublePrefs), true);
		assert.equal(bpmMatchesCompatiblePrefs(100, 180, halfDoublePrefs), true);
		assert.equal(bpmMatchesCompatiblePrefs(106, 180, halfDoublePrefs), false);
		assert.equal(bpmMatchesCompatiblePrefs(256, 128, halfDoublePrefs), true);
		assert.equal(isAppropriateNext({ key: '8A', bpm: 90 }, { key: '8A', bpm: 180 }), true);
	});

	it('requires Camelot family + BPM window', () => {
		const { isAppropriateNext } = mod;
		const ref = { key: '8A', bpm: 124 };
		assert.equal(isAppropriateNext({ key: '8A', bpm: 124 }, ref), true);
		assert.equal(isAppropriateNext({ key: '9A', bpm: 124 }, ref), true);
		assert.equal(isAppropriateNext({ key: '8B', bpm: 124 }, ref), true);
		assert.equal(isAppropriateNext({ key: '10A', bpm: 124 }, ref), false);
		assert.equal(isAppropriateNext({ key: '8A', bpm: 127 }, ref), true);
		assert.equal(isAppropriateNext({ key: '8A', bpm: 100 }, ref), false);
		assert.equal(isAppropriateNext({ key: null, bpm: 124 }, ref), false);
	});

	it('honors configurable BPM window (default ±20)', () => {
		const { isAppropriateNext, bpmMatchesCompatiblePrefs, COMPATIBLE_FILTER_DEFAULTS } = mod;
		const ref = { key: '8A', bpm: 128 };
		assert.equal(isAppropriateNext({ key: '8A', bpm: 127 }, ref), true);
		assert.equal(isAppropriateNext({ key: '8A', bpm: 100 }, ref), false);
		assert.equal(
			bpmMatchesCompatiblePrefs(106, 100, {
				...COMPATIBLE_FILTER_DEFAULTS,
				bpm_window_bpm: 6,
				bpm_enabled: true
			}),
			true
		);
	});

	it('same BPM mode uses equality tolerance, not the symmetric window or half/double', () => {
		const { bpmMatchesCompatiblePrefs, COMPATIBLE_FILTER_DEFAULTS, SAME_BPM_TOLERANCE } = mod;
		const samePrefs = {
			...COMPATIBLE_FILTER_DEFAULTS,
			bpm_direction: 'same',
			bpm_window_bpm: 20,
			allow_half_double: true,
			bpm_enabled: true
		};
		assert.equal(SAME_BPM_TOLERANCE, 0.1);
		assert.equal(bpmMatchesCompatiblePrefs(128, 128, samePrefs), true);
		assert.equal(bpmMatchesCompatiblePrefs(128.05, 128, samePrefs), true);
		assert.equal(bpmMatchesCompatiblePrefs(110, 128, samePrefs), false);
		assert.equal(bpmMatchesCompatiblePrefs(127, 128, samePrefs), false);
		assert.equal(bpmMatchesCompatiblePrefs(64, 128, samePrefs), false);
		const bothPrefs = { ...samePrefs, bpm_direction: 'both' };
		assert.equal(bpmMatchesCompatiblePrefs(110, 128, bothPrefs), true);
	});

	it('applies the BPM direction to half and double matches by raw tempo', () => {
		// Sol P2 on PR #4014: with allow_half_double on, the fold branch used to
		// admit 64 under `above` and 256 under `below` against 128.
		const { bpmMatchesCompatiblePrefs, COMPATIBLE_FILTER_DEFAULTS } = mod;
		const base = {
			...COMPATIBLE_FILTER_DEFAULTS,
			bpm_window_bpm: 6,
			allow_half_double: true,
			bpm_enabled: true
		};
		const above = { ...base, bpm_direction: 'above' };
		const below = { ...base, bpm_direction: 'below' };
		const both = { ...base, bpm_direction: 'both' };
		assert.equal(bpmMatchesCompatiblePrefs(64, 128, above), false, 'half time is below 128');
		assert.equal(bpmMatchesCompatiblePrefs(256, 128, above), true, 'double time is above 128');
		assert.equal(bpmMatchesCompatiblePrefs(256, 128, below), false, 'double time is above 128');
		assert.equal(bpmMatchesCompatiblePrefs(64, 128, below), true, 'half time is below 128');
		// Control: `both` keeps both folds.
		assert.equal(bpmMatchesCompatiblePrefs(64, 128, both), true);
		assert.equal(bpmMatchesCompatiblePrefs(256, 128, both), true);
	});
});
