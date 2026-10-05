/**
 * Source and label checks for the Show in playlists track menu (LIBM-29).
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

test('showInPlaylistsMenuItem uses the exact Show in playlists label', async () => {
	const menu = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-playlists-menu.ts'
	);
	assert.equal(menu.SHOW_IN_PLAYLISTS_LABEL, 'Show in playlists');
	assert.equal(menu.showInPlaylistsMenuItem(() => {}).label, 'Show in playlists');
	assert.equal(menu.showInPlaylistsMenuItem(() => {}).id, 'show-in-playlists');
});

test('TrackContextMenu wires the popover and menu helper without BrowserPanel edits', () => {
	const trackTable = fs.readFileSync(
		path.join(FRONTEND_ROOT, 'src/lib/components/rb/browser/TrackTable.svelte'),
		'utf8'
	);
	const trackContextMenu = fs.readFileSync(
		path.join(FRONTEND_ROOT, 'src/lib/components/rb/browser/TrackContextMenu.svelte'),
		'utf8'
	);
	const browserPanel = fs.readFileSync(
		path.join(FRONTEND_ROOT, 'src/lib/components/rb/BrowserPanel.svelte'),
		'utf8'
	);
	// TrackTable mounts the popover through TrackRowPopovers, which fetches it
	// with a dynamic import on the pick that opens it (#3886 bundle budget).
	const trackRowPopovers = fs.readFileSync(
		path.join(FRONTEND_ROOT, 'src/lib/components/rb/browser/TrackRowPopovers.svelte'),
		'utf8'
	);

	assert.match(trackContextMenu, /showInPlaylistsMenuItem/);
	assert.match(trackTable, /<TrackRowPopovers bind:playlistsMenu/);
	assert.match(trackRowPopovers, /import\('\.\/TrackPlaylistsPopover\.svelte'\)/);
	assert.match(trackRowPopovers, /<PlaylistsPopover/);
	assert.match(trackTable, /TrackContextMenu/);
	assert.doesNotMatch(browserPanel, /Show in playlists/);
});
