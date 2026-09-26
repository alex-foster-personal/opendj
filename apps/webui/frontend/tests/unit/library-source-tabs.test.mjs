// requirement: LIBM-127
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

test('library source tabs list Playlists, Taglists, Autolists, USBs in order', () => {
	const tabs = source('src/lib/components/rb/browser/LibrarySourceTabs.svelte');
	const nav = source('src/lib/components/rb/browser/LibraryNav.svelte');
	const combined = `${tabs}\n${nav}`;
	const playlists = combined.indexOf('Playlists');
	const taglists = combined.indexOf('Taglists');
	const autolists = combined.indexOf('Autolists');
	const usbs = combined.indexOf('USBs');
	assert.ok(playlists >= 0 && taglists > playlists && autolists > taglists && usbs > autolists);
});

test('PlaylistTree column mode uses data-testid playlist-column-view without a full-width toggle row', () => {
	const tree = source('src/lib/components/rb/browser/PlaylistTree.svelte');
	const tabs = source('src/lib/components/rb/browser/LibrarySourceTabs.svelte');
	assert.doesNotMatch(tree, /class="view-toggle"/);
	assert.match(tree, /data-testid="playlist-column-view"/);
	assert.match(tabs, /playlist-tree-view-toggle/);
});

test('LibraryNav mounts taglists, autolists, usbs, and playlists bodies', () => {
	const nav = source('src/lib/components/rb/browser/LibraryNav.svelte');
	assert.match(nav, /TaglistTree/);
	assert.match(nav, /TreeSmartlistSection/);
	assert.match(nav, /UsbSourceList/);
	assert.match(nav, /PlaylistTree/);
});

test('BrowserPanel loads taglists through cursor-paged fillAllTracksPane', () => {
	const panel = source('src/lib/components/rb/BrowserPanel.svelte');
	const loadStart = panel.indexOf('async function _loadPane(');
	const loadEnd = panel.indexOf('/** Reconstructs the minimal PlaylistNode');
	assert.ok(loadStart >= 0 && loadEnd > loadStart);
	const slice = panel.slice(loadStart, loadEnd);
	assert.match(slice, /kind === 'taglist'/);
	assert.match(slice, /listTracksHydrated\([\s\S]*tag:/);
	assert.match(slice, /fillAllTracksPane/);
});

test('TaglistTree fetches mytags and exposes taglist rows', () => {
	const tree = source('src/lib/components/rb/browser/TaglistTree.svelte');
	assert.match(tree, /data-testid="taglist-row"/);
	assert.match(tree, /listMyTags/);
});

test('listTracksHydrated accepts an optional tag filter', () => {
	const api = source('src/lib/rb/api-rb.ts');
	assert.match(api, /tag\?: string/);
});
