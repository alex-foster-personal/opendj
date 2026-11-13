import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let cols;
let sort;
let pane;

before(async () => {
	cols = await loadTypeScriptModule('src/lib/rb/library-columns.ts');
	sort = await loadTypeScriptModule('src/lib/components/rb/browser/browser-sort-ipc.ts');
	pane = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
});

describe('library file path columns', () => {
	it('lists File name and Location in the chooser hidden by default', () => {
		assert.equal(cols.HIDEABLE_COLUMN_LABELS.filename, 'File name');
		assert.equal(cols.HIDEABLE_COLUMN_LABELS.location, 'Location');
		assert.deepEqual(cols.DEFAULT_HIDDEN_LIBRARY_COLUMNS, [
			'album',
			'filename',
			'location',
			'date_added',
			'bitrate',
			'kind',
			'last_played'
		]);
	});

	it('registers filename and location as browser sort keys', () => {
		assert.ok(sort.BROWSER_SORT_KEYS.includes('filename'));
		assert.ok(sort.BROWSER_SORT_KEYS.includes('location'));
	});

	it('sorts rows by file name with case folding', () => {
		const rows = [
			{ stable_id: 'a', file_path: '/x/Zebra.mp3' },
			{ stable_id: 'b', file_path: '/x/alpha.mp3' }
		];
		const sorted = pane.sortRows(rows, 'filename', 1);
		assert.deepEqual(
			sorted.map((r) => r.stable_id),
			['b', 'a']
		);
	});
});
