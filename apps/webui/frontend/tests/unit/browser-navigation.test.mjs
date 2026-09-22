// requirement: IOPIN-01
// [if] browser navigation receives arrows/WASD outside an editable target [then] it moves the shared browser selection, [else stop].
// [if] browser navigation is left/right [then] focus progresses playlist -> tracks -> deck target without relying on incidental DOM focus, [else stop].
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/browser-navigation.ts');
});

describe('browser navigation policy', () => {
	it('maps arrows and WASD to a directional selection step', () => {
		for (const key of ['ArrowDown', 's', 'S']) assert.equal(mod.browserSelectionDelta(key), 1);
		for (const key of ['ArrowUp', 'w', 'W']) assert.equal(mod.browserSelectionDelta(key), -1);
		assert.equal(mod.browserSelectionDelta('Enter'), null);
	});

	it('moves focus only across the documented playlist, tracks, and deck-target zones', () => {
		assert.equal(mod.moveBrowserFocus('playlist', 'ArrowRight'), 'tracks');
		assert.equal(mod.moveBrowserFocus('tracks', 'ArrowRight'), 'deck');
		assert.equal(mod.moveBrowserFocus('deck', 'ArrowRight'), 'deck');
		assert.equal(mod.moveBrowserFocus('deck', 'ArrowLeft'), 'tracks');
		assert.equal(mod.moveBrowserFocus('tracks', 'ArrowLeft'), 'playlist');
	});

	it('refuses to steal key events from inputs and context menus', () => {
		assert.equal(mod.browserNavigationMayHandle({ editable: true, contextMenuOpen: false }), false);
		assert.equal(mod.browserNavigationMayHandle({ editable: false, contextMenuOpen: true }), false);
		assert.equal(mod.browserNavigationMayHandle({ editable: false, contextMenuOpen: false }), true);
	});
});
