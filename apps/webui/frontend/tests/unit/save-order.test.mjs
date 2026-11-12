import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * "Save this order" (LIBUX-8678).
 *
 * [if] a key sort is shown [then] the saved ids follow the shown order, else LIBUX-8678 is broken.
 * [if] a filter or collapsed section hides rows [then] they still go to the server, else the save is refused.
 * [if] the shown order equals the stored one [then] the button stays hidden, else LIBUX-8678 is broken.
 */
let mod;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/library/save-order.ts');
});

const rows = (...ids) => ids.map((item_id) => ({ item_id }));

describe('save this order', () => {
	it('saves the shown order', () => {
		assert.deepEqual(mod.saveOrderItemIds(rows('c', 'a', 'b'), rows('a', 'b', 'c')), [
			'c',
			'a',
			'b'
		]);
	});

	it('keeps hidden rows, in their stored place after the shown ones', () => {
		assert.deepEqual(mod.saveOrderItemIds(rows('c', 'a'), rows('a', 'b', 'c', 'd')), [
			'c',
			'a',
			'b',
			'd'
		]);
	});

	it('refuses a row with no item_id', () => {
		assert.throws(() => mod.saveOrderItemIds(rows('a', null), rows('a', null)), /no item_id/);
	});

	it('shows the button only when the orders differ', () => {
		assert.equal(mod.orderDiffers(rows('a', 'b'), rows('a', 'b')), false);
		assert.equal(mod.orderDiffers(rows('b', 'a'), rows('a', 'b')), true);
	});
});
