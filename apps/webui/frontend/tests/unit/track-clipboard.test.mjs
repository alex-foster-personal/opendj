/**
 * Library edit shortcuts: Cmd/Ctrl+A select all, Cmd/Ctrl+C/X/V copy, cut
 * and paste tracks between playlists (pin ce142ae7e22f in #3988, duplicated
 * as 0b1e12cc01d0 in #3989; LIBM-160..LIBM-162).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const BROWSER_PANEL = `${FRONTEND_ROOT}/src/lib/components/rb/BrowserPanel.svelte`;

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/browser/track-clipboard.ts');
});

function key(k, opts = {}) {
	return {
		key: k,
		metaKey: opts.meta ?? true,
		ctrlKey: opts.ctrl ?? false,
		altKey: opts.alt ?? false,
		shiftKey: opts.shift ?? false,
		target: opts.target ?? { tagName: 'TR', getAttribute: () => null }
	};
}

function pane(rows, sel = {}) {
	return {
		rows,
		selected_id: sel.id ?? null,
		selected_ids: sel.ids ?? [],
		selected_order: sel.order ?? null,
		selected_orders: sel.orders ?? []
	};
}

const ROWS = [
	{ stable_id: 'a', order: 1 },
	{ stable_id: 'b', order: 2 },
	{ stable_id: 'c', order: 3 },
	{ stable_id: 'a', order: 4 }
];

test('Cmd and Ctrl with A, C, X, V map to the four library actions', () => {
	assert.equal(mod.libraryEditShortcut(key('a')), 'select_all');
	assert.equal(mod.libraryEditShortcut(key('A')), 'select_all');
	assert.equal(mod.libraryEditShortcut(key('c')), 'copy');
	assert.equal(mod.libraryEditShortcut(key('x')), 'cut');
	assert.equal(mod.libraryEditShortcut(key('v', { meta: false, ctrl: true })), 'paste');
});

test('plain letters, Shift, Alt and other keys are not library shortcuts', () => {
	assert.equal(mod.libraryEditShortcut(key('a', { meta: false })), null);
	assert.equal(mod.libraryEditShortcut(key('a', { shift: true })), null);
	assert.equal(mod.libraryEditShortcut(key('c', { alt: true })), null);
	assert.equal(mod.libraryEditShortcut(key('z')), null);
	assert.equal(mod.libraryEditShortcut(key('f')), null);
});

test('a text field keeps native Cmd+A/C/V', () => {
	const input = { tagName: 'INPUT', type: 'search', getAttribute: () => null };
	const textarea = { tagName: 'TEXTAREA', getAttribute: () => null };
	assert.equal(mod.libraryEditShortcut(key('a', { target: input })), null);
	assert.equal(mod.libraryEditShortcut(key('v', { target: textarea })), null);
	// Control: a non-text input (a checkbox in the table) still gets the shortcut.
	const checkbox = { tagName: 'INPUT', type: 'checkbox', getAttribute: () => null };
	assert.equal(mod.libraryEditShortcut(key('a', { target: checkbox })), 'select_all');
});

test('select all selects every rendered row, duplicates by position', () => {
	const p = pane(ROWS);
	assert.equal(mod.selectAllRows(p, ROWS), 3);
	assert.deepEqual(p.selected_orders, [1, 2, 3, 4]);
	assert.deepEqual(p.selected_ids, ['a', 'b', 'c']);
	assert.equal(p.selected_id, 'a');
	assert.equal(p.selected_order, 1);
});

test('select all covers only what is on screen, not filtered-out rows', () => {
	const p = pane(ROWS);
	const visible = [ROWS[2], ROWS[1]];
	assert.equal(mod.selectAllRows(p, visible), 2);
	assert.deepEqual(p.selected_orders, [3, 2]);
	assert.deepEqual(p.selected_ids, ['c', 'b']);
});

test('select all keeps an on-screen anchor and replaces an off-screen one', () => {
	const kept = pane(ROWS, { id: 'c', order: 3, ids: ['c'], orders: [3] });
	mod.selectAllRows(kept, ROWS);
	assert.equal(kept.selected_id, 'c');
	assert.equal(kept.selected_order, 3);

	const moved = pane(ROWS, { id: 'a', order: 4, ids: ['a'], orders: [4] });
	mod.selectAllRows(moved, ROWS.slice(0, 3));
	assert.equal(moved.selected_id, 'a');
	assert.equal(moved.selected_order, 1);
});

test('select all on an empty list changes nothing', () => {
	const p = pane([], { id: 'x', ids: ['x'] });
	assert.equal(mod.selectAllRows(p, []), 0);
	assert.deepEqual(p.selected_ids, ['x']);
});

test('copied ids follow the on-screen order, not the click order', () => {
	assert.deepEqual(mod.selectedIdsInViewOrder(ROWS, [3, 1], ['c', 'a']), ['a', 'c']);
	assert.deepEqual(mod.selectedIdsInViewOrder(ROWS, [1, 4], ['a']), ['a']);
	// No positional selection (column view): fall back to selected_ids.
	assert.deepEqual(mod.selectedIdsInViewOrder(ROWS, [], ['b', 'b', 'a']), ['b', 'a']);
	// A selected row filtered off screen is still copied.
	assert.deepEqual(mod.selectedIdsInViewOrder([ROWS[0]], [1, 2], ['a', 'b']), ['a', 'b']);
});

test('paste needs a clipboard and a real playlist in the collection view', () => {
	const clip = { stable_ids: ['a'], mode: 'copy', source_playlist_id: null, source_title: 'x' };
	const playlist = { playlist_id: 'p1', kind: 'playlist', whole_collection: false };
	assert.equal(mod.pasteBlockReason(playlist, 'collection', clip), null);
	assert.match(mod.pasteBlockReason(playlist, 'collection', null), /nothing to paste/);
	assert.match(
		mod.pasteBlockReason(playlist, 'collection', { ...clip, stable_ids: [] }),
		/nothing to paste/
	);
	assert.match(mod.pasteBlockReason(playlist, 'spotify', clip), /open a playlist/);
	for (const kind of ['all_tracks', 'smartlist', 'folder', 'missing_tracks', 'taglist', 'autolist']) {
		assert.match(
			mod.pasteBlockReason({ ...playlist, kind }, 'collection', clip),
			/open a playlist/,
			kind
		);
	}
	assert.match(
		mod.pasteBlockReason({ ...playlist, playlist_id: 'all' }, 'collection', clip),
		/open a playlist/
	);
	assert.match(
		mod.pasteBlockReason({ ...playlist, whole_collection: true }, 'collection', clip),
		/whole-collection/
	);
});

test('paste skips tracks the target already holds', () => {
	assert.deepEqual(mod.partitionPaste(['a', 'b', 'c'], ['b']), { add: ['a', 'c'], already: 1 });
	assert.deepEqual(mod.partitionPaste(['a', 'a'], []), { add: ['a'], already: 1 });
	assert.deepEqual(mod.partitionPaste(['a'], ['a']), { add: [], already: 1 });
});

test('pasted rows are the last row of each pasted track, in slot order', () => {
	const rows = [
		{ stable_id: 'a', order: 1 },
		{ stable_id: 'b', order: 2 },
		{ stable_id: 'c', order: 3 },
		{ stable_id: 'b', order: 4 }
	];
	assert.deepEqual(mod.pastedRowOrders(rows, ['b', 'c']), [3, 4]);
	assert.deepEqual(mod.pastedRowOrders(rows, ['z']), []);
});

test('selectRowOrders selects exactly those rows and anchors on the first', () => {
	const p = pane(ROWS, { id: 'a', order: 1, ids: ['a'], orders: [1] });
	mod.selectRowOrders(p, [4, 3]);
	assert.deepEqual(p.selected_orders, [3, 4]);
	assert.deepEqual(p.selected_ids, ['c', 'a']);
	assert.equal(p.selected_id, 'c');
	assert.equal(p.selected_order, 3);
	// Nothing matching leaves the selection alone.
	mod.selectRowOrders(p, [99]);
	assert.deepEqual(p.selected_orders, [3, 4]);
});

test('the reveal scroll puts the pasted row two rows below the top', () => {
	assert.equal(mod.pasteRevealScrollTop(0, 'compact'), 0);
	assert.equal(mod.pasteRevealScrollTop(2, 'compact'), 0);
	assert.equal(mod.pasteRevealScrollTop(80, 'compact'), 78 * 22 + 20);
	assert.equal(mod.pasteRevealScrollTop(80, 'cosy'), 78 * 30 + 20);
});

test('toast copy names counts and what happens next', () => {
	assert.equal(
		mod.clipboardToastMessage(1, 'copy'),
		'Copied 1 track - open a playlist and press Cmd+V to paste'
	);
	assert.match(mod.clipboardToastMessage(3, 'cut'), /^Cut 3 tracks/);
	assert.equal(mod.pasteToastMessage(2, 0, 'Warmup', false), 'Pasted 2 tracks into "Warmup"');
	assert.equal(
		mod.pasteToastMessage(1, 2, 'Warmup', true),
		'Moved 1 track into "Warmup" (2 already there)'
	);
	assert.equal(
		mod.pasteToastMessage(0, 3, 'Warmup', false),
		'Nothing new to paste into "Warmup" (3 already there)'
	);
});

test('the clipboard is shared module state', () => {
	mod.setTrackClipboard(null);
	assert.equal(mod.getTrackClipboard(), null);
	const clip = { stable_ids: ['a'], mode: 'cut', source_playlist_id: 'p', source_title: 'P' };
	mod.setTrackClipboard(clip);
	assert.deepEqual(mod.getTrackClipboard(), clip);
	mod.setTrackClipboard(null);
});

test('BrowserPanel installs and removes the edit-shortcut listener', () => {
	const src = readFileSync(BROWSER_PANEL, 'utf8');
	assert.match(src, /window\.addEventListener\('keydown', onLibraryEditKey\)/);
	assert.match(src, /window\.removeEventListener\('keydown', onLibraryEditKey\)/);
	// Paste goes through the set-union transfer endpoint, not a full rewrite.
	const paste = src.slice(src.indexOf('async function _pasteTracks'));
	assert.match(paste.slice(0, 4000), /transferPlaylistTracks\(/);
	assert.doesNotMatch(paste.slice(0, 4000), /replacePlaylistTracks\(/);
	// Every pane showing the destination reloads, then the paste is revealed.
	assert.match(paste.slice(0, 4000), /q\.playlist_id === destId/);
	assert.match(paste.slice(0, 4000), /_revealPasted\(p, plan\.add\)/);
});
