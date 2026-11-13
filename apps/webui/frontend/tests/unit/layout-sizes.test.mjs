import assert from 'node:assert/strict';
import test from 'node:test';

import { migrateTreeWidth, parseLayoutSizes, sizeFor } from '../../src/lib/layout/layout-sizes.ts';

test('legacy playlist_tree_width migrates into layout[skin][browser.tree]', () => {
	const result = migrateTreeWidth({ playlist_tree_width: 300 }, 'classic');
	assert.equal(result.migrated, true);
	assert.equal(result.layout.classic['browser.tree'], 300);
});

test('existing tree width is not overwritten by the legacy field', () => {
	const prefs = { playlist_tree_width: 300, layout: { classic: { 'browser.tree': 220 } } };
	const result = migrateTreeWidth(prefs, 'classic');
	assert.equal(result.migrated, false);
	assert.equal(result.layout.classic['browser.tree'], 220);
});

test('parse drops non finite or non positive px and reports region ids', () => {
	const { map, dropped } = parseLayoutSizes({
		classic: { 'browser.tree': 240, 'deck.a': -5, 'deck.b': Number.NaN, mixer: 'wide' }
	});
	assert.deepEqual(map, { classic: { 'browser.tree': 240 } });
	assert.deepEqual(dropped.sort(), ['deck.a', 'deck.b', 'mixer']);
});

test('sizeFor returns undefined for a missing region, not another skin value', () => {
	const map = { classic: { 'browser.tree': 240 }, compact: {} };
	assert.equal(sizeFor(map, 'compact', 'browser.tree'), undefined);
	assert.equal(sizeFor(map, 'absent', 'browser.tree'), undefined);
	assert.equal(sizeFor(map, 'classic', 'browser.tree'), 240);
});
