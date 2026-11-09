import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://layout-regions.example.test';

let layout;

before(async () => {
	layout = await loadTypeScriptModule('src/lib/rb/layout-regions.ts', {
		viteApiBase: API_BASE
	});
});

test('[if] fresh prefs [then] layoutView matches browser.tree bounds and collapsed false', () => {
	const v = layout.layoutView();
	assert.equal(typeof v.skin, 'string');
	assert.ok(v.skin.length > 0);
	const r = v.regions['browser.tree'];
	assert.deepEqual(r, { px: 300, min: 220, max: 520, collapsed: false });
});

test('[if] setLayoutSize then resetLayout [then] px follows and restores', () => {
	layout.setLayoutSize('browser.tree', 400);
	assert.equal(layout.layoutView().regions['browser.tree'].px, 400);
	layout.resetLayout('browser.tree');
	assert.equal(layout.layoutView().regions['browser.tree'].px, 300);
	layout.setLayoutSize('browser.tree', 400);
	layout.resetLayout();
	assert.equal(layout.layoutView().regions['browser.tree'].px, 300);
});

test('[if] setLayoutSize out of range or fractional [then] throws and does not move px', () => {
	const beforePx = layout.layoutView().regions['browser.tree'].px;
	for (const px of [9999, 219, 521, 300.5]) {
		assert.throws(() => layout.setLayoutSize('browser.tree', px), /220\.\.520/);
	}
	assert.equal(layout.layoutView().regions['browser.tree'].px, beforePx);
});

test('[if] parseLayoutRegion names unknown [then] message lists browser.tree', () => {
	assert.throws(() => layout.parseLayoutRegion('browser.nope'), /browser\.tree/);
});
