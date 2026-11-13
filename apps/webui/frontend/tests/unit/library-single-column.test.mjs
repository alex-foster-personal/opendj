import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Single-column library layout (LIBUX-9743, issue #9743) (rekordbox browser layout toggle, mimicked).
 *
 * [if] single-column leaves a tree column [then] the track table never gets the full width - broken.
 * [if] leaving single-column loses the tree width [then] the DJ re-drags the divider every time - broken.
 * [if] the choice is not in the persisted prefs blob [then] a reload brings the tree back - broken.
 * [if] library_single_column misses the pref or the snapshot [then] agents cannot drive or read the layout - broken.
 * [if] the explainer is still planned [then] the live button reads as not built - broken.
 */

// The engine reads the page URL for ?extroute=; these tests run as a page with no query.
globalThis.location ??= { search: '' };

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

let layout;
let explainers;

before(async () => {
	layout = await loadTypeScriptModule('src/lib/components/rb/browser/browser-layout.ts');
	explainers = await loadTypeScriptModule('src/lib/rb/planned-explainers.ts');
});

function _storage(values) {
	return {
		getItem: (key) => values.get(key) ?? null,
		setItem: (key, value) => values.set(key, value),
		removeItem: (key) => values.delete(key)
	};
}

async function _withWindow(values, body) {
	const original = globalThis.window;
	globalThis.window = { localStorage: _storage(values) };
	try {
		return await body();
	} finally {
		if (original === undefined) delete globalThis.window;
		else globalThis.window = original;
	}
}

describe('browser grid columns', () => {
	it('single-column zeroes the tree and its divider so the list fills the width', () => {
		/** [if] single-column is on [then] tree and divider columns are 0px and the list keeps 1fr, [else stop]. */
		assert.equal(layout.browserGridColumns(true, 300), '30px 0px 0px minmax(0, 1fr)');
	});

	it('leaving single-column puts the tree back at its stored width', () => {
		/** [if] single-column goes off with a stored width of 300 [then] the tree column is 300px again, [else stop]. */
		assert.equal(layout.browserGridColumns(false, 300), '30px 300px 6px minmax(0, 1fr)');
		assert.throws(() => layout.browserGridColumns(false, Number.NaN), RangeError);
	});
});

describe('single-column persistence', () => {
	it('the choice survives a reload through the prefs store and leaves the tree width alone', async () => {
		/** [if] single-column is set then the prefs module reloads [then] it is still on and the tree width is unchanged, [else stop]. */
		const values = new Map([
			[STORAGE_KEY, JSON.stringify({ hide_broken_links: false, playlist_tree_width: 300 })]
		]);
		await _withWindow(values, async () => {
			const first = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
			assert.equal(
				first.uiPrefs.library_single_column,
				false,
				'an old blob without the key loads as off'
			);
			first.setLibrarySingleColumn(true);
			assert.equal(JSON.parse(values.get(STORAGE_KEY)).library_single_column, true);
			assert.throws(() => first.setLibrarySingleColumn('yes'), TypeError);
			const reloaded = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
			assert.equal(reloaded.uiPrefs.library_single_column, true);
			assert.equal(reloaded.uiPrefs.playlist_tree_width, 300);
		});
	});
});

describe('library_single_column performance command', () => {
	it('sets, flips and reports the layout without the UI', async () => {
		/** [if] library_single_column runs with enabled and without [then] the pref and library_panels.single_column follow, [else stop]. */
		await _withWindow(new Map(), async () => {
			const bus = await loadTypeScriptModule('tests/unit/fixtures/pairing-snapshot-entry.ts', {
				viteApiBase: 'https://single-column.example.test'
			});
			const uninstall = bus.installPerformanceBrowserIpc();
			try {
				assert.equal(bus.queryPerformanceState().library_panels.single_column, false);
				await bus.dispatchPerformanceCommand({ type: 'library_single_column', enabled: true });
				assert.equal(bus.uiPrefs.library_single_column, true);
				assert.equal(bus.queryPerformanceState().library_panels.single_column, true);
				await bus.dispatchPerformanceCommand({ type: 'library_single_column' });
				assert.equal(bus.queryPerformanceState().library_panels.single_column, false);
			} finally {
				uninstall();
			}
		});
	});

	it('refuses a non-boolean or an unknown field and never queues behind a deck', async () => {
		/** [if] enabled is "yes" or a stray field rides along [then] the parser refuses it and the scope is null, [else stop]. */
		const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
		assert.throws(
			() => ipc.parsePerformanceCommandForTest({ type: 'library_single_column', enabled: 'yes' }),
			/boolean/
		);
		assert.throws(
			() => ipc.parsePerformanceCommandForTest({ type: 'library_single_column', panel: 'tree' }),
			/unexpected fields/
		);
		assert.equal(ipc.performanceCommandQueueScopes({ type: 'library_single_column' }), null);
	});
});

describe('single-column explainer', () => {
	it('moves to the built catalogue with the planned wording minus the not-built line', () => {
		/** [if] single-column-layout is still planned or its wording changed [then] the live button misdescribes itself, [else stop]. */
		assert.equal(explainers.PLANNED_CONTROLS['single-column-layout'], undefined);
		assert.equal(
			explainers.builtTitle('single-column-layout'),
			'Single-column library - hides the playlist tree and gives the track table the full browser width.'
		);
	});
});
