import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let snap;

const VALID = {
	name: 'my desk',
	skin: 'green-room',
	layout: { performance: 'auto', library: 'three-panel' },
	regions: { 'browser.tree': 400 }
};

before(async () => {
	snap = await loadTypeScriptModule('src/lib/rb/layout-snapshots.ts', {
		viteApiBase: 'https://layout-snapshots.example.test'
	});
});

test('[if] a snapshot names an unknown region id [then] parseLayoutSnapshot throws naming the allowed region ids', () => {
	assert.throws(
		() => snap.parseLayoutSnapshot({ ...VALID, regions: { 'deck.left': 300 } }),
		/region must be one of browser\.tree; got deck\.left/
	);
});

test('[if] a region px is outside its min..max [then] it throws with the allowed range and does not clamp', () => {
	assert.throws(
		() => snap.parseLayoutSnapshot({ ...VALID, regions: { 'browser.tree': 9999 } }),
		/within 220\.\.520; got 9999/
	);
	assert.throws(
		() => snap.snapshotFromState('x', 'default', VALID.layout, { 'browser.tree': 100 }),
		/within 220\.\.520; got 100/
	);
});

test('[if] a snapshot layout block holds an unknown preset [then] it throws with the layoutPresetChoices list', async () => {
	const skinLayout = await loadTypeScriptModule('src/lib/rb/skin-layout.ts', {
		viteApiBase: 'https://layout-snapshots.example.test'
	});
	const list = skinLayout.layoutPresetChoices('library').join('|');
	assert.throws(
		() =>
			snap.parseLayoutSnapshot({
				...VALID,
				layout: { performance: 'auto', library: 'sidebar-left' }
			}),
		(err) => err.message.includes(list) && err.message.includes('sidebar-left')
	);
});

test('[if] parseLayoutSnapshot(JSON round trip of s) differs from s for a valid snapshot [then] the round-trip test fails', () => {
	const s = snap.snapshotFromState(VALID.name, VALID.skin, VALID.layout, VALID.regions);
	assert.deepEqual(s, VALID);
	assert.deepEqual(snap.parseLayoutSnapshot(JSON.parse(JSON.stringify(s))), s);
});
