import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LIBUX-13928: the Key column funnel (issue #9739) (rekordbox Track Filter Key, mimicked).
 *
 * [if] the funnel does not list the 24 keys, or labels them in another notation [then] it does not match the cells - broken.
 * [if] picking several keys drops a row in one of them or keeps another key [then] the funnel lies - broken.
 * [if] a notation switch changes or drops the picks [then] picks were stored as display strings - broken.
 * [if] clearing does not restore every row, or one pane's picks leak into another [then] the funnel is not per pane - broken.
 */

let notation;
let filters;
let store;

const ROWS = [
	{ stable_id: 'eight-a', key: '8A' },
	{ stable_id: 'am-spelled', key: 'Am' },
	{ stable_id: 'nine-a', key: '9A' },
	{ stable_id: 'eight-b', key: '8B' },
	{ stable_id: 'no-key', key: null },
	{ stable_id: 'junk', key: 'zz' }
];

const ids = (rows) => rows.map((r) => r.stable_id);
const withKey = (key) => ({ ...filters.EMPTY_TRACK_TABLE_COLUMN_FILTERS, key });

before(async () => {
	notation = await loadTypeScriptModule('src/lib/rb/key-notation.ts');
	filters = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-table-column-filters.ts'
	);
	store = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-table-funnel-store.svelte.ts'
	);
});

describe('track-table Key funnel', () => {
	it('lists the 24 Camelot keys, each labelled in the active notation', () => {
		assert.equal(notation.CAMELOT_KEYS.length, 24);
		assert.equal(new Set(notation.CAMELOT_KEYS).size, 24);
		assert.deepEqual(notation.CAMELOT_KEYS.slice(0, 2), ['1A', '2A']);
		assert.equal(notation.CAMELOT_KEYS[12], '1B');
		assert.equal(notation.formatKey('8A', 'musical'), 'Am');
		assert.equal(notation.formatKey('8A', 'open_key'), '1m');
		const musical = notation.CAMELOT_KEYS.map((k) => notation.formatKey(k, 'musical'));
		assert.equal(new Set(musical).size, 24);
	});

	it('keeps rows in any picked key, whatever notation the row key is stored in', () => {
		let key = filters.EMPTY_KEY_FILTER;
		key = filters.toggleKeySelection(key, '8A');
		key = filters.toggleKeySelection(key, '9A');
		assert.equal(filters.keyFilterActive(key), true);
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, withKey(key))), [
			'eight-a',
			'am-spelled',
			'nine-a'
		]);
		key = filters.toggleKeySelection(key, '9A');
		assert.deepEqual(key.selected, ['8A']);
	});

	it('stores picks as canonical keys, so a pick made in any notation is the same pick', () => {
		const viaMusical = filters.toggleKeySelection(filters.EMPTY_KEY_FILTER, 'Am');
		const viaOpenKey = filters.toggleKeySelection(filters.EMPTY_KEY_FILTER, '1m');
		const viaCamelotMusical = filters.toggleKeySelection(
			filters.EMPTY_KEY_FILTER,
			notation.formatKey('8A', 'camelot_musical')
		);
		for (const picked of [viaMusical, viaOpenKey, viaCamelotMusical]) {
			assert.deepEqual(picked.selected, ['8A']);
		}
		assert.equal(filters.toggleKeySelection(viaMusical, 'zz'), viaMusical);
	});

	it('clears back to every row and keeps each pane its own picks', () => {
		const s = new store.TrackTableFunnelStore();
		s.patchKey('pane-a', filters.toggleKeySelection(filters.EMPTY_KEY_FILTER, '8B'));
		assert.deepEqual(s.get('pane-b').key, filters.EMPTY_KEY_FILTER);
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, s.get('pane-a'))), ['eight-b']);
		s.patchKey('pane-a', filters.toggleKeySelection(s.get('pane-a').key, '8B'));
		assert.equal(filters.keyFilterActive(s.get('pane-a').key), false);
		assert.equal(filters.applyTrackTableColumnFilters(ROWS, s.get('pane-a')), ROWS);
	});
});
