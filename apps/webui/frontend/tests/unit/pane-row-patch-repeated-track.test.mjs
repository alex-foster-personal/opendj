/**
 * A playlist may hold one track many times (memberships, not distinct tracks).
 * A whole-row patch for that track must map EACH membership row on its own: the
 * old patch copied the first row over every repeat, so all of them carried the
 * first row's item_id and order. TrackTable keys rows by item_id, so the copies
 * raised each_key_duplicate and the table stopped updating (a paste into a long
 * playlist never scrolled its row into view, main E2E run 43364041357).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let contract;
before(async () => {
	contract = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
});

function _member(item_id, order) {
	return { stable_id: 'sid-a', item_id, order, revealed: false, rating: 1 };
}

test('[if] a patched track repeats in a playlist [then] every repeat keeps its own item_id and order [else stop]', () => {
	const pane = contract.createPaneStore();
	pane.rows = [_member('item-1', 1), _member('item-2', 2), _member('item-3', 3)];
	contract.updatePaneRow(pane, 'sid-a', (row) => ({ ...row, revealed: true }));
	if (pane.rows.map((r) => r.item_id).join(',') !== 'item-1,item-2,item-3') {
		console.log('if updatePaneRow copies the first row over the repeats then broken');
	}
	assert.deepEqual(
		pane.rows.map((r) => [r.item_id, r.order, r.revealed]),
		[
			['item-1', 1, true],
			['item-2', 2, true],
			['item-3', 3, true]
		]
	);
});

test('[if] a membership row and its search result share a track [then] the search row is not replaced by the membership row [else stop]', () => {
	const pane = contract.createPaneStore();
	pane.rows = [_member('item-1', 1)];
	pane.search_results = [_member(null, 7)];
	contract.updatePaneRow(pane, 'sid-a', (row) => ({ ...row, rating: 5 }));
	assert.deepEqual([pane.search_results[0].item_id, pane.search_results[0].order], [null, 7]);
	assert.equal(pane.search_results[0].rating, 5);
});

test('[if] the map returns the rows it was given [then] nothing is published [else stop]', () => {
	const pane = contract.createPaneStore();
	const rows = [_member('item-1', 1), _member('item-2', 2)];
	pane.rows = rows;
	contract.updatePaneRow(pane, 'sid-a', (row) => row);
	assert.equal(pane.rows, rows);
});
