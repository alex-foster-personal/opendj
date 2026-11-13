import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Split-column library layout (LIBUX-9754, issue #9754) (rekordbox browser split view, mimicked).
 *
 * [if] the two tables do not each keep their own pane, sort and selection [then] the second table is a mirror - broken.
 * [if] clicking the right table swaps the columns round [then] the DJ loses which list is where - broken.
 * [if] the second table opens on an empty pane [then] the DJ sees a blank list - broken.
 * [if] a drop onto a read-only table is not refused with a reason [then] rows vanish silently - broken.
 * [if] the choice is not in the persisted prefs blob [then] a reload drops back to one table - broken.
 * [if] library_split_column misses the pref or the snapshot [then] agents cannot drive or read the layout - broken.
 * [if] the explainer is still planned [then] the live button reads as not built - broken.
 */

// The engine reads the page URL for ?extroute=; these tests run as a page with no query.
globalThis.location ??= { search: '' };

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

let split;
let paneContract;
let explainers;

before(async () => {
	split = await loadTypeScriptModule('src/lib/components/rb/browser/split-column.ts');
	paneContract = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-contract.svelte.ts'
	);
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

describe('split-column panes', () => {
	it('shows the active pane and the pinned right pane, each with its own sort and selection', () => {
		/** [if] split shows panes A and B [then] sorting and selecting A leaves B as it was, [else stop]. */
		const a = paneContract.createPaneStore();
		const b = paneContract.createPaneStore();
		const shown = split.splitPanes([a, b], a, b, null);
		assert.equal(shown.left, a);
		assert.equal(shown.right, b);
		a.toggleSort('bpm');
		a.select('t1');
		assert.equal(a.sort_key, 'bpm');
		assert.deepEqual(a.selected_ids, ['t1']);
		assert.equal(b.sort_key, null);
		assert.deepEqual(b.selected_ids, []);
	});

	it('activating the right table keeps the left one where it was', () => {
		/** [if] the right pane becomes active [then] the left column still shows the last left pane, [else stop]. */
		const [a, b, c] = ['a', 'b', 'c'];
		assert.deepEqual(split.splitPanes([a, b, c], b, b, c), { left: c, right: b });
		assert.deepEqual(split.splitPanes([a, b, c], b, b, null), { left: a, right: b });
		assert.equal(split.splitPanes([b], b, b, null), null, 'one pane alone cannot split');
		assert.equal(split.splitPanes([a, c], a, b, null), null, 'a closed right pane ends the split');
		assert.equal(split.pickSplitRight([a, b, c], b), c);
		assert.equal(split.pickSplitRight([a, b], b), a);
		assert.equal(
			split.pickSplitRight([a], a),
			null,
			'no neighbour tab: planSplitRight decides what the second table opens'
		);
	});

	it('the second table always opens on a real source, never a blank pane', () => {
		/** [if] split opens with no neighbour tab [then] the second table opens the library root (All Tracks), not an empty pane, [else stop]. */
		const [a, b] = ['a', 'b'];
		assert.deepEqual(split.planSplitRight([a, b], a, true), { kind: 'pin', pane: b });
		assert.deepEqual(split.planSplitRight([a, b], b, false), { kind: 'pin', pane: a });
		const opened = split.planSplitRight([a], a, true);
		assert.deepEqual(opened, { kind: 'open', playlist_id: 'all' });
		assert.equal(opened.playlist_id, split.SPLIT_ROOT_PLAYLIST_ID);
		assert.equal(
			split.planSplitRight([a], a, false),
			null,
			'no free tab slot: split waits, no pane is added'
		);
	});

	it('appends a drop only onto a writable playlist table and names the reason otherwise', () => {
		/** [if] rows are dropped on a read-only, non-playlist or empty table [then] it is refused with a reason, [else stop]. */
		const playlist = { kind: 'playlist', playlist_id: 'p1', title: 'Warmup', writable: true };
		assert.equal(split.splitDropRefusal(playlist, 2), null);
		assert.match(
			split.splitDropRefusal({ ...playlist, writable: false }, 2),
			/Warmup cannot be changed/
		);
		assert.match(
			split.splitDropRefusal({ ...playlist, kind: 'all_tracks', title: 'All Tracks' }, 2),
			/All Tracks is not a playlist/
		);
		assert.match(split.splitDropRefusal({ ...playlist, playlist_id: null }, 2), /no list open/);
		assert.match(split.splitDropRefusal(playlist, 0), /nothing was dragged/);
	});
});

describe('split-column persistence', () => {
	it('the choice survives a reload through the prefs store', async () => {
		/** [if] split-column is set then the prefs module reloads [then] it is still on, [else stop]. */
		const values = new Map([[STORAGE_KEY, JSON.stringify({ hide_broken_links: false })]]);
		await _withWindow(values, async () => {
			const first = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
			assert.equal(
				first.uiPrefs.library_split_column,
				false,
				'an old blob without the key loads as off'
			);
			first.setLibrarySplitColumn(true);
			assert.equal(JSON.parse(values.get(STORAGE_KEY)).library_split_column, true);
			assert.throws(() => first.setLibrarySplitColumn('yes'), TypeError);
			const reloaded = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
			assert.equal(reloaded.uiPrefs.library_split_column, true);
		});
	});
});

describe('library_split_column performance command', () => {
	it('sets, flips and reports the layout without the UI', async () => {
		/** [if] library_split_column runs with enabled and without [then] the pref and library_panels.split_column follow, [else stop]. */
		await _withWindow(new Map(), async () => {
			const bus = await loadTypeScriptModule('tests/unit/fixtures/pairing-snapshot-entry.ts', {
				viteApiBase: 'https://split-column.example.test'
			});
			const uninstall = bus.installPerformanceBrowserIpc();
			try {
				assert.equal(bus.queryPerformanceState().library_panels.split_column, false);
				await bus.dispatchPerformanceCommand({ type: 'library_split_column', enabled: true });
				assert.equal(bus.uiPrefs.library_split_column, true);
				assert.equal(bus.queryPerformanceState().library_panels.split_column, true);
				await bus.dispatchPerformanceCommand({ type: 'library_split_column' });
				assert.equal(bus.queryPerformanceState().library_panels.split_column, false);
			} finally {
				uninstall();
			}
		});
	});

	it('refuses a non-boolean or an unknown field and never queues behind a deck', async () => {
		/** [if] enabled is "yes" or a stray field rides along [then] the parser refuses it and the scope is null, [else stop]. */
		const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
		assert.throws(
			() => ipc.parsePerformanceCommandForTest({ type: 'library_split_column', enabled: 'yes' }),
			/boolean/
		);
		assert.throws(
			() => ipc.parsePerformanceCommandForTest({ type: 'library_split_column', pane: 1 }),
			/unexpected fields/
		);
		assert.equal(ipc.performanceCommandQueueScopes({ type: 'library_split_column' }), null);
	});
});

describe('split-column explainer', () => {
	it('moves to the built catalogue and names the agent verb', () => {
		/** [if] split-column-layout is still planned or does not name its verb [then] the live button misdescribes itself, [else stop]. */
		assert.equal(explainers.PLANNED_CONTROLS['split-column-layout'], undefined);
		assert.match(
			explainers.builtTitle('split-column-layout'),
			/^Split-column library - .*opendj library_split_column\.$/
		);
	});
});
