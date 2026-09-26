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
});
