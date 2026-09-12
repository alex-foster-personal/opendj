import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as Picker } from '$lib/components/rb/browser/AddToPlaylistPicker.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function playlist(index) {
	return {
		playlist_id: `pl-${index}`,
		name: `Playlist ${index}`,
		track_count: index,
		broken_count: 0,
		kind: 'playlist',
		children: []
	};
}

function renderPicker(overrides = {}) {
	return mod.render(mod.Picker, {
		props: {
			open: true,
			playlists: [],
			onpick: () => {},
			onclose: () => {},
			...overrides
		}
	}).body;
}

test('16 playlists: filter input present and names render', () => {
	const playlists = Array.from({ length: 16 }, (_, i) => playlist(i + 1));
	const html = renderPicker({ playlists });
	assert.match(html, /aria-label="Filter playlists"/);
	assert.match(html, /Playlist 1/);
	assert.match(html, /Playlist 16/);
});

test('15 playlists: filter input absent and names still render', () => {
	const playlists = Array.from({ length: 15 }, (_, i) => playlist(i + 1));
	const html = renderPicker({ playlists });
	assert.equal(html.includes('Filter playlists'), false);
	assert.match(html, /Playlist 1/);
	assert.match(html, /Playlist 15/);
});

test('dialog markup uses buttons without draggable', () => {
	const html = renderPicker({ playlists: [playlist(1)] });
	assert.match(html, /role="dialog"/);
	assert.match(html, /data-testid="add-to-playlist-picker"/);
	assert.match(html, /type="button"/);
	assert.equal(html.includes('draggable="true"'), false);
});

test('open false renders no dialog', () => {
	const html = mod.render(mod.Picker, {
		props: {
			open: false,
			playlists: [playlist(1)],
			onpick: () => {},
			onclose: () => {}
		}
	}).body;
	assert.equal(html.includes('add-to-playlist-picker'), false);
});
