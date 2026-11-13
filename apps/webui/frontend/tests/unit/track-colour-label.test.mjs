import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LIBUX-13890: the rekordbox track colour label (issue #9737), its row menu group and the Colour column sort.
 *
 * [if] the row menu lacks the 8 colours plus None, or a colour item is disabled [then] no colour can be set - broken.
 * [if] runSetColour PATCHes the wrong field, skips a row or writes a stick track [then] the selection is not coloured - broken.
 * [if] the Colour column does not sort by ColorID with uncoloured rows last [then] it does not match rekordbox - broken.
 * [if] the swatch or name for a ColorID drifts from rekordbox's order [then] the dot shows the wrong colour - broken.
 */

let registry;
let menuMod;
let filters;
let pane;
let sort;

const TRACK_CTX = {
	selection: 'many',
	source: 'collection',
	audio: 'present',
	stems: 'none',
	tracklistHidden: 'shown',
	workspace: 'library'
};

before(async () => {
	registry = await loadTypeScriptModule('src/lib/context-menu/registry.ts');
	menuMod = await loadTypeScriptModule('src/lib/components/rb/browser/track-context-menu.ts');
	filters = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-table-column-filters.ts'
	);
	pane = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	sort = await loadTypeScriptModule('src/lib/components/rb/browser/browser-sort-ipc.ts');
});

describe('track colour label', () => {
	it('offers the 8 rekordbox colours then None, all enabled, each naming its PATCH', () => {
		const items = registry
			.menuFor({ kind: 'track', count: 3, rows: 3 }, TRACK_CTX)
			.filter((item) => item.id.startsWith('colour-'));
		assert.deepEqual(
			items.map((item) => item.label),
			[
				'Colour: Pink',
				'Colour: Red',
				'Colour: Orange',
				'Colour: Yellow',
				'Colour: Green',
				'Colour: Aqua',
				'Colour: Blue',
				'Colour: Purple',
				'Colour: None'
			]
		);
		assert.ok(items.every((item) => item.enabled));
		assert.equal(items[0].title, 'PATCH /api/v1/tracks/{stable_id} {"color_id": 1}');
		assert.equal(items[8].title, 'PATCH /api/v1/tracks/{stable_id} {"color_id": null}');
		for (const source of ['usb', 'readonly-import']) {
			const readOnly = registry.menuFor(
				{ kind: 'track', count: 1, rows: 1 },
				{ ...TRACK_CTX, source }
			);
			assert.equal(
				readOnly.find((item) => item.id.startsWith('colour-')),
				undefined,
				source
			);
		}
	});

	it('PATCHes color_id on every selected row with its ETag and skips stick tracks', async () => {
		const patches = [];
		const toasts = [];
		await menuMod.runSetColour(['a', 'b', 'usb-aaaaaaaa-bbbbbbbb-1'], 6, {
			getTrack: async (id) => ({ etag: `"e-${id}"`, track: {} }),
			patchTrack: async (id, etag, body) => {
				patches.push({ id, etag, body });
				return { track: {} };
			},
			pushToast: (message, kind) => toasts.push({ message, kind })
		});
		assert.deepEqual(patches, [
			{ id: 'a', etag: '"e-a"', body: { color_id: 6 } },
			{ id: 'b', etag: '"e-b"', body: { color_id: 6 } }
		]);
		assert.deepEqual(
			toasts.map((t) => t.kind),
			['info', 'error']
		);
		assert.match(toasts[0].message, /Colour set \(Aqua\) on 2 tracks/);

		const cleared = [];
		await menuMod.runSetColour(['a'], null, {
			getTrack: async () => ({ etag: '"e"', track: {} }),
			patchTrack: async (id, _etag, body) => {
				cleared.push({ id, body });
				return { track: {} };
			},
			pushToast: () => {}
		});
		assert.deepEqual(cleared, [{ id: 'a', body: { color_id: null } }]);
	});

	it('sorts the Colour column by ColorID, uncoloured rows last both ways', () => {
		assert.ok(sort.BROWSER_SORT_KEYS.includes('colour'));
		const rows = [
			{ stable_id: 'blue', title: 'A', color_id: 7 },
			{ stable_id: 'none', title: 'B', color_id: null },
			{ stable_id: 'pink', title: 'C', color_id: 1 },
			{ stable_id: 'zero', title: 'D', color_id: 0 },
			{ stable_id: 'green', title: 'E', color_id: 5 }
		];
		const ids = (dir) => pane.sortRows(rows, 'colour', dir).map((r) => r.stable_id);
		assert.deepEqual(ids(1).slice(0, 3), ['pink', 'green', 'blue']);
		assert.deepEqual(ids(-1).slice(0, 3), ['blue', 'green', 'pink']);
		assert.deepEqual(new Set(ids(1).slice(3)), new Set(['none', 'zero']));
		assert.deepEqual(new Set(ids(-1).slice(3)), new Set(['none', 'zero']));
	});

	it('maps each ColorID to its rekordbox name and swatch, none for 0 or null', () => {
		assert.equal(filters.trackColourName(1), 'Pink');
		assert.equal(filters.trackColourName(8), 'Purple');
		assert.equal(filters.trackColourName(0), 'No colour');
		assert.equal(filters.trackColourName(null), 'No colour');
		assert.equal(filters.trackColourSwatch(1), '#ff007f');
		assert.equal(filters.trackColourSwatch(2), '#ff0000');
		assert.equal(filters.trackColourSwatch(null), null);
	});
});
