import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as View } from '$lib/components/rb/browser/SpotifySourcePanelView.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function renderView(overrides = {}) {
	return mod.render(mod.View, {
		props: {
			playlists: [],
			playlistsLoading: false,
			playlistsError: null,
			selectedId: 'spotify:inv',
			pendingTracks: [],
			loading: false,
			error: null,
			pinnedIds: [],
			recentIds: [],
			purchaseSources: ['beatport'],
			account: null,
			accountLoading: false,
			accountError: null,
			connectNote: null,
			playerNote: null,
			connectHere: true,
			nativeShell: false,
			sidePlayerDocked: false,
			shrinkMain: false,
			clientIdSaving: false,
			oncollection: () => {},
			onselect: () => {},
			ontogglepin: () => {},
			onrefresh: () => {},
			onconnect: () => {},
			onplay: () => {},
			onimportlogins: () => {},
			onshrinkchange: () => {},
			onsaveclientid: async () => true,
			onopenexternal: () => {},
			ownedTracks: [{ title: 'Owned', artist: 'Artist', album: null }],
			ownedLoading: false,
			...overrides
		}
	}).body;
}

test('Already owned heading and owned mark are not the green spotify brand mark', () => {
	const html = renderView({
		playlists: [
			{
				playlist_id: 'spotify:inv',
				name: 'Invented',
				track_count: 1,
				available_count: 1,
				updated_at: '2026-11-05T00:00:00Z',
				vendor: 'spotify',
				vendor_pl_id: 'inv'
			}
		]
	});
	assert.match(html, /Already owned/);
	assert.match(html, /in library/);
	assert.match(html, /owned-mark/);
	assert.match(html, /spotify-mark/);
});
