/**
 * ARTTHUMB-8728c / ARTTHUMB-8728d: the playlist art view option and its tiles.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

let ssr;

before(async () => {
	ssr = await loadSvelteSsrModule([
		"export { default as PlaylistTreeView } from '$lib/components/rb/browser/PlaylistTreeView.svelte';",
		"export { default as meta } from '$lib/components/rb/browser/PlaylistTreeView.stories';",
		"export * as art from '$lib/rb/library-sidebar-art.svelte';",
		"export { render } from 'svelte/server';"
	].join('\n'));
});

function body(sidebarart) {
	return ssr.render(ssr.PlaylistTreeView, { props: { ...ssr.meta.args, sidebarart } }).body;
}

test('ARTTHUMB-8728c tiles show only with the option on, sized 48 px with a hover title', () => {
	const on = body(true);
	const tiles = on.match(/<img[^>]*data-testid="playlist-art-tile"[^>]*>/g) ?? [];
	assert.ok(tiles.length > 0, 'option on renders a tile per playlist row');
	for (const tile of tiles) {
		assert.match(tile, /src="[^"]*\/api\/v1\/playlists\/[^"]+\/thumbnail\?size=64"/);
		assert.match(tile, /width="48"/);
		assert.match(tile, /height="48"/);
		assert.match(tile, /loading="lazy"/);
		assert.match(tile, /title="Playlist art for /);
		assert.doesNotMatch(tile, /\/artwork/, 'the artwork grayscale rule must not catch the tile');
	}
	assert.match(on, /data-testid="playlists-grid-link"/);
	assert.match(on, /Playlist - \d+ tracks/);
	const off = body(false);
	assert.doesNotMatch(off, /playlist-art-tile/, 'option off renders no tile');
	assert.doesNotMatch(off, /playlists-grid-link/);
});

test('ARTTHUMB-8728c the flag gates the option; unread or absent flags show nothing', () => {
	const { sidebarArtEnabledIn, playlistThumbnailUrl } = ssr.art;
	assert.equal(sidebarArtEnabledIn([]), false);
	assert.equal(sidebarArtEnabledIn([{ flag_id: 'library.art_thumbnails', enabled: false }]), false);
	assert.equal(sidebarArtEnabledIn([{ flag_id: 'library.art_thumbnails', enabled: true }]), true);
	assert.match(playlistThumbnailUrl('a b/c', 232), /\/api\/v1\/playlists\/a%20b%2Fc\/thumbnail\?size=232$/);
});
