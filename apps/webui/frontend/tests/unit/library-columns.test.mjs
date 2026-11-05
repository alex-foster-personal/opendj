import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { afterEach, before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LIBUX-54 column chooser: right-click the track table header to show or hide
 * columns; visibility persists in ui-prefs `library_hidden_columns`.
 *
 * [if] toggling a column twice does not restore the list [then] the checkbox lies - broken.
 * [if] the stored list is not in table order [then] two equal sets can differ on disk - broken.
 * [if] an unknown or repeated id is accepted [then] a corrupt blob silently changes the table - broken.
 * [if] a hidden column still counts toward the table width [then] the MASTER badge drifts - broken.
 * [if] toggleLibraryColumn does not PUT [then] the choice is lost on the next launch - broken.
 * [if] a hideable column has no <col> guard or CSS rule [then] its cells and header misalign - broken.
 */

const FRONTEND = fileURLToPath(new URL('../../', import.meta.url));
const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const API_BASE = 'https://library-columns.example.test';
let cols;
let widths;

before(async () => {
	cols = await loadTypeScriptModule('src/lib/rb/library-columns.ts');
	widths = await loadTypeScriptModule('src/lib/rb/library-column-widths.ts');
});

afterEach(() => {
	delete globalThis.window;
});

describe('library-columns', () => {
	it('never offers the utility, title, autoplay or lyrics columns', () => {
		for (const id of ['funnel', 'err', 'cloud', 'order', 'title', 'autoplay', 'lyrics']) {
			assert.equal(cols.isHideableColumn(id), false, id);
		}
		assert.equal(cols.HIDEABLE_COLUMNS.length, 15);
		for (const col of cols.HIDEABLE_COLUMNS) assert.ok(cols.HIDEABLE_COLUMN_LABELS[col], col);
	});

	it('toggles a column off and back on, keeping table order', () => {
		const off = cols.toggleHiddenColumn(['stems'], 'key');
		assert.deepEqual(off, ['key', 'stems']);
		assert.deepEqual(cols.toggleHiddenColumn(off, 'key'), ['stems']);
	});

	it('validates a stored list into table order and refuses bad ones', () => {
		assert.deepEqual(cols.validateHiddenColumns(['stems', 'art'], 'test'), ['art', 'stems']);
		assert.deepEqual(cols.validateHiddenColumns([], 'test'), []);
		assert.throws(() => cols.validateHiddenColumns(['title'], 'test'), /not one of/);
		assert.throws(() => cols.validateHiddenColumns(['key', 'key'], 'test'), /twice/);
		assert.throws(() => cols.validateHiddenColumns('key', 'test'), /must be a list/);
		assert.throws(() => cols.validateHiddenColumns(undefined, 'test'), /must be a list/);
	});

	it('zeroes hidden widths and leaves the rest', () => {
		const w = { title: 220, key: 64, bpm: 46 };
		assert.deepEqual(widths.shownColumnWidths(w, ['key']), { title: 220, key: 0, bpm: 46 });
		assert.deepEqual(w, { title: 220, key: 64, bpm: 46 }, 'input must not be mutated');
	});

	it('names one class per hidden column', () => {
		assert.equal(widths.hiddenColumnClasses(['key', 'bpm']), 'hide-key hide-bpm');
		assert.equal(widths.hiddenColumnClasses([]), '');
	});
});

describe('toggleLibraryColumn', () => {
	it('persists locally and PUTs the new list', async () => {
		const store = new Map();
		globalThis.window = {
			localStorage: {
				getItem: (k) => (store.has(k) ? store.get(k) : null),
				setItem: (k, v) => store.set(k, v)
			}
		};
		const bodies = [];
		const original = globalThis.fetch;
		globalThis.fetch = async (request) => {
			bodies.push(await request.clone().json());
			return new Response(JSON.stringify({ theme: 'dark' }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		};
		try {
			const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
			prefs.toggleLibraryColumn('key');
			assert.deepEqual(prefs.uiPrefs.library_hidden_columns, ['key']);
			assert.deepEqual(JSON.parse(store.get(STORAGE_KEY)).library_hidden_columns, ['key']);
			prefs.showAllLibraryColumns();
			assert.deepEqual(prefs.uiPrefs.library_hidden_columns, []);
			await new Promise((resolve) => setTimeout(resolve, 0));
			assert.deepEqual(bodies, [{ library_hidden_columns: ['key'] }, { library_hidden_columns: [] }]);
		} finally {
			globalThis.fetch = original;
		}
	});
});

describe('GET /ui-prefs hydrates library_hidden_columns', () => {
	const ok = (body) =>
		new Response(JSON.stringify({ key_notation: 'camelot', library_key_filter: [], ...body }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});

	async function _prefsWith(body) {
		const store = new Map();
		globalThis.window = {
			localStorage: {
				getItem: (k) => (store.has(k) ? store.get(k) : null),
				setItem: (k, v) => store.set(k, v)
			}
		};
		const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
		const original = globalThis.fetch;
		globalThis.fetch = async () => ok(body);
		return { prefs, restore: () => (globalThis.fetch = original) };
	}

	it('applies the served list', async () => {
		const { prefs, restore } = await _prefsWith({ library_hidden_columns: ['stems', 'key'] });
		try {
			await prefs.hydrateConfirmPrefsFromDisk();
			assert.deepEqual(prefs.uiPrefs.library_hidden_columns, ['key', 'stems']);
		} finally {
			restore();
		}
	});

	for (const [label, body] of [
		['a missing', { library_watcher_folders: ['/x'] }],
		['an unknown column in a', { library_hidden_columns: ['title'], library_watcher_folders: ['/x'] }]
	]) {
		it(`${label} list fails hydration, keeps the old value and still hydrates the rest`, async () => {
			const { prefs, restore } = await _prefsWith(body);
			try {
				prefs.uiPrefs.library_hidden_columns = ['art'];
				await assert.rejects(prefs.hydrateConfirmPrefsFromDisk(), /library_hidden_columns/);
				assert.deepEqual(prefs.uiPrefs.library_hidden_columns, ['art']);
				assert.deepEqual(prefs.uiPrefs.library_watcher_folders, ['/x']);
			} finally {
				restore();
			}
		});
	}
});

describe('TrackTable wiring', () => {
	const table = readFileSync(join(FRONTEND, 'src/lib/components/rb/browser/TrackTable.svelte'), 'utf8');

	it('every hideable column has a <col> guard and a hide rule for header and cells', () => {
		for (const col of cols.HIDEABLE_COLUMNS) {
			assert.ok(table.includes(`{#if !hiddenCols.has('${col}')}<col`), `<col> guard for ${col}`);
			assert.ok(
				table.includes(`table.hide-${col} :is(th.h-${col}, td.c-${col})`),
				`hide rule for ${col}`
			);
		}
	});

	it('the header opens the chooser and the sums use the shown widths', () => {
		assert.match(table, /<thead[^>]*oncontextmenu=\{\(e\) => trackContextMenu\?\.openColumnChooser\(e, columnChooserPrefs\)\}/);
		const host = readFileSync(join(FRONTEND, 'src/lib/components/rb/browser/TrackContextMenu.svelte'), 'utf8');
		assert.match(host, /export function openColumnChooser\(event: MouseEvent, prefs: ColumnChooserPrefs\)/);
		assert.match(host, /<ColumnChooserMenu /);
		// [if] a track menu opens while the chooser is open [then] two menus stack [else stop]
		assert.match(host, /chooserAt = null;\s*menu = \{/);
		const chooser = readFileSync(join(FRONTEND, 'src/lib/components/rb/browser/ColumnChooserMenu.svelte'), 'utf8');
		for (const setter of ['toggleLibraryColumn', 'showAllLibraryColumns', 'setKeyNotation', 'setLibraryDensity', 'setUiSkin']) {
			assert.match(table, new RegExp(`\\b${setter}\\b`), `the table must hand the chooser ${setter}`);
		}
		for (const call of ['toggleColumn', 'showAllColumns', 'setKeyNotation', 'setDensity', 'setSkin']) {
			assert.match(chooser, new RegExp(`prefs\\.${call}\\b`), `chooser must offer ${call}`);
		}
		assert.doesNotMatch(chooser, /from '\$lib\/rb\/prefs\.svelte'/, 'the chooser must not import prefs.svelte (fan-in ceiling)');
		assert.match(table, /Object\.entries\(shownWidths\)/);
		assert.doesNotMatch(table, /masterFoldCenterPx\(colWidths/);
	});
});
