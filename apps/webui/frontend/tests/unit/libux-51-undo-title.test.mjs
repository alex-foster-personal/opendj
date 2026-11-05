/**
 * LIBUX-51 (the maintainer, Tue 6 Oct 2026: "Hovering on undo button does not show what
 * it will do"): the undo button's title names the edit the next undo reverses,
 * the entry just below the history cursor, and says "Nothing to undo" when
 * there is none. The real LibrarySourceTabs is rendered with svelte SSR (see
 * load-svelte-ssr.mjs for what that can and cannot prove).
 */
// requirement: LIBUX-51
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as Tabs } from '$lib/components/rb/browser/LibrarySourceTabs.svelte';",
	"export { playlistHistoryChrome } from '$lib/rb/playlist-history-chrome.svelte';",
	"export { undoButtonTitle } from '$lib/rb/playlist-history';",
	"export { render } from 'svelte/server';"
].join('\n');

const entry = (n, label) => ({ command_id: `c${n}`, op: 'add', playlist_id: 'p1', ts: '2026-10-06T17:00:00Z', label });
const ENTRIES = [entry(1, 'Rename Warmup to Opening'), entry(2, 'Add 3 tracks to Opening'), entry(3, 'Delete Old Set')];

let ssr;

before(async () => {
	ssr = await loadSvelteSsrModule(ENTRY);
});

function undoButtonTitleAttr(state) {
	Object.assign(ssr.playlistHistoryChrome, state);
	const html = ssr.render(ssr.Tabs, { props: { active: 'playlists', onchange: () => {} } }).body;
	const button = html.match(/<button\b[^>]*data-testid="playlist-undo"[^>]*>/)?.[0];
	assert.ok(button, 'the undo button must render');
	return button.match(/\btitle="([^"]*)"/)?.[1];
}

test('LIBUX-51: the undo title names the entry just below the cursor', () => {
	const title = undoButtonTitleAttr({ entries: ENTRIES, cursor: 2, canUndo: true, canRedo: true });
	assert.equal(title, 'Undo: Add 3 tracks to Opening (Ctrl/Cmd+Z)');
});

test('LIBUX-51: the title follows the cursor, not the newest entry', () => {
	assert.equal(
		undoButtonTitleAttr({ entries: ENTRIES, cursor: 3, canUndo: true, canRedo: false }),
		'Undo: Delete Old Set (Ctrl/Cmd+Z)'
	);
	assert.equal(
		undoButtonTitleAttr({ entries: ENTRIES, cursor: 1, canUndo: true, canRedo: true }),
		'Undo: Rename Warmup to Opening (Ctrl/Cmd+Z)'
	);
});

test('LIBUX-51: with nothing to undo the title says so', () => {
	assert.equal(undoButtonTitleAttr({ entries: [], cursor: 0, canUndo: false, canRedo: false }), 'Nothing to undo');
	assert.equal(undoButtonTitleAttr({ entries: ENTRIES, cursor: 0, canUndo: false, canRedo: true }), 'Nothing to undo');
});

test('LIBUX-51 control: an undo the window cannot name falls back to the generic wording, never a wrong edit', () => {
	assert.equal(ssr.undoButtonTitle([], 4, true), 'Undo playlist edit (Ctrl/Cmd+Z)');
	assert.equal(ssr.undoButtonTitle(ENTRIES, 0, true), 'Undo playlist edit (Ctrl/Cmd+Z)');
});

// Popover position, found by the v1 ship day thread (#5674) and folded in here.
const TABS_SRC = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/LibrarySourceTabs.svelte', import.meta.url)),
	'utf8'
);

test('LIBUX-51: the history popover renders inside the positioned toolbar, so it opens under the undo button', () => {
	const markup = TABS_SRC.slice(0, TABS_SRC.indexOf('<style>')).replace(/<!--[\s\S]*?-->/g, '');
	const toolbar = markup.indexOf('<div class="source-toolbar"');
	const popover = markup.indexOf('class="history-popover"');
	assert.ok(toolbar >= 0 && popover > toolbar, 'the popover comes after the toolbar opens');
	const between = markup.slice(toolbar, popover);
	const open = (between.match(/<div\b/g) ?? []).length - (between.match(/<\/div>/g) ?? []).length;
	assert.ok(open >= 2, 'the toolbar div is still open where the popover renders (toolbar + popover div)');
	assert.match(TABS_SRC, /\.source-toolbar \{[^}]*position: relative;/);
});

test('LIBUX-51 superseded by LIBUX-8626: the popover is interactive but only after a deliberate hover, and closes once the pointer leaves', () => {
	const button = TABS_SRC.slice(TABS_SRC.indexOf('data-testid="playlist-undo"'), TABS_SRC.indexOf('data-testid="playlist-redo"'));
	assert.match(button, /onmouseenter=\{intendHistory\}/);
	assert.match(button, /onmouseleave=\{releaseHistory\}/);
	assert.match(button, /onblur=\{releaseHistoryOnBlur\}/);
});
