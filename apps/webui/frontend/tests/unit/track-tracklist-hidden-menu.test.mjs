import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// NOWP-11487: Hide in tracklists context menu item.
// Regression lines:
// - if the menu label does not flip with tracklistHidden then broken
// - if runSetTracklistHidden PATCHes the wrong field then broken

let registry;
let menuMod;

before(async () => {
	registry = await loadTypeScriptModule('src/lib/context-menu/registry.ts');
	menuMod = await loadTypeScriptModule('src/lib/components/rb/browser/track-context-menu.ts');
});

// REQ: NOWP-11487
test('menu label is Hide or Show in tracklists from tracklistHidden', () => {
	const shown = registry.menuFor(
		{ kind: 'track', count: 1, rows: 1 },
		{
			selection: 'one',
			source: 'collection',
			audio: 'present',
			stems: 'none',
			tracklistHidden: 'shown',
			workspace: 'library'
		}
	);
	const hidden = registry.menuFor(
		{ kind: 'track', count: 1, rows: 1 },
		{
			selection: 'one',
			source: 'collection',
			audio: 'present',
			stems: 'none',
			tracklistHidden: 'hidden',
			workspace: 'library'
		}
	);
	const shownItem = shown.find((item) => item.id === 'tracklist-hidden');
	const hiddenItem = hidden.find((item) => item.id === 'tracklist-hidden');
	assert.equal(shownItem?.label, 'Hide in tracklists');
	assert.equal(hiddenItem?.label, 'Show in tracklists');
	assert.equal(shownItem?.enabled, true);
	assert.equal(
		hidden.find((item) => item.id === 'tracklist-hidden' && item.enabled === false),
		undefined
	);
});

test('runSetTracklistHidden PATCHes tracklist_hidden and skips USB ids', async () => {
	const patches = [];
	await menuMod.runSetTracklistHidden(['native-id', 'usb-aaaaaaaa-bbbbbbbb-1'], true, {
		getTrack: async (id) => ({ etag: `"e-${id}"`, track: {} }),
		patchTrack: async (id, _etag, body) => {
			patches.push({ id, body });
			return { track: {} };
		},
		pushToast: () => {}
	});
	assert.deepEqual(patches, [{ id: 'native-id', body: { tracklist_hidden: true } }]);
});

test('usb and readonly-import hide the tracklist item', () => {
	for (const source of ['usb', 'readonly-import']) {
		const menu = registry.menuFor(
			{ kind: 'track', count: 1, rows: 1 },
			{
				selection: 'one',
				source,
				audio: 'present',
				stems: 'none',
				tracklistHidden: 'shown',
				workspace: 'library'
			}
		);
		assert.equal(
			menu.find((item) => item.id === 'tracklist-hidden'),
			undefined,
			source
		);
	}
});
