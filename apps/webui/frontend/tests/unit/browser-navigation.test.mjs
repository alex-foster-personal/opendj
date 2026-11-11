// requirement: IOPIN-01, KEYB-11643
// [if] browser navigation receives arrows/WASD outside an editable target [then] it moves the shared browser selection, [else stop].
// [if] browser navigation is left/right [then] focus progresses playlist -> tracks -> deck target without relying on incidental DOM focus, [else stop].
// [if] Shift+Right with a selected row does not map to load deck 2 [then] broken.
// [if] Cmd+Shift+Left does not map to instant double from deck 2 [then] broken.
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

describe('library deck chords (KEYB-11643)', () => {
	const load = (extra) =>
		mod.libraryChordAction({
			shiftKey: true,
			metaKey: false,
			ctrlKey: false,
			altKey: false,
			...extra
		});
	const modded = (extra) =>
		mod.libraryChordAction({
			shiftKey: false,
			metaKey: true,
			ctrlKey: false,
			altKey: false,
			...extra
		});

	it('maps Shift+arrows to deck loads and Cmd/Ctrl chords to double or locate', () => {
		assert.deepEqual(load({ key: 'ArrowLeft' }), { kind: 'load', deck: 1 });
		assert.deepEqual(load({ key: 'ArrowRight' }), { kind: 'load', deck: 2 });
		assert.deepEqual(
			mod.libraryChordAction({
				key: 'ArrowLeft',
				shiftKey: true,
				metaKey: true,
				ctrlKey: false,
				altKey: false
			}),
			{ kind: 'double', from: 2, to: 1 }
		);
		assert.deepEqual(
			mod.libraryChordAction({
				key: 'ArrowRight',
				shiftKey: true,
				metaKey: false,
				ctrlKey: true,
				altKey: false
			}),
			{ kind: 'double', from: 1, to: 2 }
		);
		assert.deepEqual(modded({ key: 'l' }), { kind: 'locate' });
		assert.deepEqual(
			mod.libraryChordAction({
				key: 'L',
				shiftKey: false,
				metaKey: false,
				ctrlKey: true,
				altKey: false
			}),
			{ kind: 'locate' }
		);
	});

	it('returns null for chords outside the library deck set', () => {
		assert.equal(
			mod.libraryChordAction({
				key: 'ArrowLeft',
				shiftKey: false,
				metaKey: false,
				ctrlKey: false,
				altKey: false
			}),
			null
		);
		assert.equal(load({ key: 'a' }), null);
		assert.equal(
			mod.libraryChordAction({
				key: 'ArrowLeft',
				shiftKey: true,
				metaKey: false,
				ctrlKey: false,
				altKey: true
			}),
			null
		);
		assert.equal(modded({ key: 'ArrowLeft' }), null);
		assert.equal(
			mod.libraryChordAction({
				key: 'l',
				shiftKey: true,
				metaKey: true,
				ctrlKey: false,
				altKey: false
			}),
			null
		);
	});

	it('locateDecision picks the recent or lowest loaded deck and pane fallback', () => {
		const empty = { 1: null, 2: null, 3: null, 4: null };
		assert.deepEqual(
			mod.locateDecision({
				recentDeck: 2,
				deckStableIds: empty,
				paneHas: () => false,
				paneIsAllTracks: false
			}),
			{ kind: 'none-loaded' }
		);
		assert.deepEqual(
			mod.locateDecision({
				recentDeck: 2,
				deckStableIds: { ...empty, 2: 'b', 1: 'a' },
				paneHas: (sid) => sid === 'b',
				paneIsAllTracks: false
			}),
			{ kind: 'reveal', deck: 2, stableId: 'b' }
		);
		assert.deepEqual(
			mod.locateDecision({
				recentDeck: 2,
				deckStableIds: { ...empty, 1: 'a' },
				paneHas: () => false,
				paneIsAllTracks: false
			}),
			{ kind: 'all-tracks', deck: 1, stableId: 'a' }
		);
		assert.deepEqual(
			mod.locateDecision({
				recentDeck: null,
				deckStableIds: { ...empty, 3: 'c' },
				paneHas: () => false,
				paneIsAllTracks: true
			}),
			{ kind: 'absent', deck: 3, stableId: 'c' }
		);
	});

	it('instantDoubleCommand is a zero-beat copy or null when the source deck is empty', () => {
		assert.deepEqual(mod.instantDoubleCommand(2, 1, 'sid'), {
			type: 'deck_move',
			from: 2,
			to: 1,
			mode: 'copy',
			crossfade_beats: 0
		});
		assert.equal(mod.instantDoubleCommand(2, 1, null), null);
	});
});
