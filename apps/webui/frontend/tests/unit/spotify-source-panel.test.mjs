/**
 * Issue #315: SpotifySourcePanel first-paint markup for the name filter,
 * visible/total count, and per-row pin. SSR cannot type into the box; this
 * proves the branches the operator sees before interaction.
 *
 * WHAT A PASS HERE DOES NOT COVER: no DOM, so this proves nothing about
 * layout, CSS, stacking, or click handlers.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as Panel } from '$lib/components/rb/browser/SpotifySourcePanel.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function playlist(overrides = {}) {
	return {
		playlist_id: 'pl-1',
		name: 'Lantern Drift',
		track_count: 12,
		available_count: 12,
		updated_at: '2026-07-24T00:00:00Z',
		vendor: 'spotify',
		...overrides
	};
}

function props(overrides = {}) {
	return {
		playlists: [],
		playlistsLoading: false,
		playlistsError: null,
		selectedId: null,
		pendingTracks: null,
		loading: false,
		error: null,
		oncollection: () => {},
		onselect: () => {},
		...overrides
	};
}

function renderPanel(overrides = {}) {
	return mod.render(mod.Panel, { props: props(overrides) }).body;
}

test('with ≥1 playlist: filter input, N / N count, pin button, Buy list still present', () => {
	const html = renderPanel({ playlists: [playlist()] });
	assert.match(html, /aria-label="Filter Spotify playlists"/);
	assert.match(html, /1 \/ 1/);
	assert.match(html, /aria-label="Pin Lantern Drift"/);
	assert.match(html, /Buy list/);
	assert.match(html, /Choose a Spotify playlist to view its persisted acquisition queue/);
});

test('pin aria-pressed is false on first paint (prefs default empty)', () => {
	const html = renderPanel({ playlists: [playlist()] });
	assert.match(html, /aria-pressed="false"/);
	assert.equal(html.includes('aria-pressed="true"'), false, `rendered: ${html}`);
});

test('with 0 playlists: empty imported copy and no filter input', () => {
	const html = renderPanel({ playlists: [] });
	assert.match(html, /No imported Spotify playlists\./);
	assert.equal(
		html.includes('Filter Spotify playlists'),
		false,
		'zero imported must not show the filter box'
	);
	assert.equal(html.includes('1 / 1'), false);
});

test('loading branch is unchanged', () => {
	const html = renderPanel({ playlistsLoading: true, playlists: [playlist()] });
	assert.match(html, /Loading imported Spotify playlists\.\.\./);
	assert.equal(html.includes('Filter Spotify playlists'), false);
});

test('an imported-list error is reported above the rows, not instead of them', () => {
	// The account's own playlists stay listed when only the
	// imported list fails. SSR has no account, so the rows here stand in for
	// whatever the merge still holds; the point is that the error does not
	// replace them.
	const html = renderPanel({ playlistsError: 'boom', playlists: [playlist()] });
	assert.match(html, /Spotify playlist load failed: boom/);
	assert.match(html, /aria-label="Filter Spotify playlists"/);
	assert.match(html, /1 \/ 1/);
	assert.match(html, /aria-label="Pin Lantern Drift"/);
});

test('an imported row with no Spotify id keeps an inert play button that says why', () => {
	// The row is not silently unplayable.
	const html = renderPanel({
		playlists: [playlist({ playlist_id: 'weird/id', vendor_pl_id: null })]
	});
	assert.match(html, /aria-label="Play Lantern Drift in the Spotify side player \(unavailable\)"/);
	assert.match(html, /disabled/);
	assert.match(html, /No Spotify playlist id was saved for this imported playlist/);
	const playable = renderPanel({
		playlists: [playlist({ playlist_id: 'spotify:abc', vendor_pl_id: 'abc' })]
	});
	assert.match(playable, /aria-label="Play Lantern Drift in the Spotify side player"/);
	assert.equal(playable.includes('(unavailable)'), false);
});

test('an imported-list error with nothing to list shows the error alone', () => {
	const html = renderPanel({ playlistsError: 'boom', playlists: [] });
	assert.match(html, /Spotify playlist load failed: boom/);
	assert.equal(html.includes('No imported Spotify playlists.'), false);
	assert.equal(html.includes('Filter Spotify playlists'), false);
});

// ----- SPOT-6555: the account listing loads through the real request path ---
// With no initialAccount the panel asks the engine. The signed-out and
// missing-client-id states run against the real engine in
// tests/e2e/spotify-connect-popup.spec.ts; the account-and-imported merge is
// tests/unit/spotify-account-merge.test.mjs. A live signed-in listing needs a
// real Spotify account and is checked on a Mac; the connected paint below
// hands the panel the engine's listing shape instead.
test('SPOT-6555: first paint asks the engine for the account and lists imported playlists meanwhile', () => {
	const html = renderPanel({ playlists: [playlist()] });
	assert.match(html, /Checking your Spotify account\.\.\./);
	assert.match(html, /1 \/ 1/);
	assert.equal(html.includes('>Connect Spotify<'), false);
});

test('SPOT-6555c: outside the desktop app the Chrome import is disabled with a reason', () => {
	const html = renderPanel({ playlists: [] });
	assert.match(html, /Import logins from Chrome/);
	assert.match(html, /disabled/);
	assert.match(html, /Desktop app only/);
});

// ----- SPOT-6555: the connected paint ---
// The engine fetch is the dependency here, so the listing is handed in as
// initialAccount; the panel's own merge, markers and row wiring are real.
function accountPlaylist(id, extra = {}) {
	return {
		spotify_id: id,
		name: `Invented ${id}`,
		owner_id: 'me',
		owner_name: null,
		owned: true,
		collaborative: false,
		public: null,
		track_count: 3,
		image_url: null,
		open_url: `https://open.spotify.com/playlist/${id}`,
		snapshot_id: null,
		...extra
	};
}

function connectedAccount(playlists) {
	return {
		connected: true,
		user_id: 'me',
		display_name: 'Invented DJ',
		total_reported: playlists.length,
		playlists
	};
}

test('SPOT-6555: a connected account names itself, counts its playlists and the imported share', () => {
	const html = renderPanel({
		initialAccount: connectedAccount([accountPlaylist('aaa'), accountPlaylist('bbb')]),
		playlists: [playlist({ playlist_id: 'spotify:bbb', name: 'Imported bbb', vendor_pl_id: 'bbb' })]
	});
	assert.equal(html.includes('Checking your Spotify account...'), false, `rendered: ${html}`);
	assert.match(html, /Invented DJ · 2 playlists/);
	assert.match(html, /1 of them are imported into Open DJ/);
	assert.match(html, /2 \/ 2/);
});

test('SPOT-6555: only the imported account row carries the imported mark and opens its Buy list', () => {
	const html = renderPanel({
		initialAccount: connectedAccount([accountPlaylist('aaa'), accountPlaylist('bbb')]),
		playlists: [playlist({ playlist_id: 'spotify:bbb', name: 'Imported bbb', vendor_pl_id: 'bbb' })]
	});
	assert.equal(html.match(/aria-label="Imported into Open DJ"/g)?.length, 1, `rendered: ${html}`);
	// The row keeps the account's live name; the import supplies the Buy list.
	assert.match(html, /Invented bbb: imported into Open DJ\. Click for its Buy list\./);
	assert.match(html, /Invented aaa: not imported\. Click to play it in the Spotify side player\./);
	assert.match(html, /class="playlist-row[^"]*not-imported/);
	assert.match(html, /aria-label="Play Invented aaa in the Spotify side player"/);
});

test('SPOT-6555: account rows show under the loading notice while the imported list is still loading', () => {
	// A slow or hung state.db request must not hide the account's own playlists.
	const html = renderPanel({
		initialAccount: connectedAccount([accountPlaylist('aaa'), accountPlaylist('bbb')]),
		playlistsLoading: true,
		playlists: [playlist({ playlist_id: 'spotify:bbb', name: 'Imported bbb', vendor_pl_id: 'bbb' })]
	});
	assert.match(html, /Loading imported Spotify playlists\.\.\./);
	assert.match(
		html,
		/aria-label="Play Invented aaa in the Spotify side player"/,
		`rendered: ${html}`
	);
	assert.match(html, /aria-label="Play Invented bbb in the Spotify side player"/);
	assert.match(html, /aria-label="Filter Spotify playlists"/);
	// Imports are not claimed until their list has landed.
	assert.equal(html.includes('aria-label="Imported into Open DJ"'), false);
	assert.equal(html.includes('Imported bbb'), false);
});

test('SPOT-6555: a connected account with no playlists says so, not the imported-only copy', () => {
	const html = renderPanel({ initialAccount: connectedAccount([]), playlists: [] });
	assert.match(html, /No playlists on this Spotify account\./);
	assert.equal(html.includes('No imported Spotify playlists.'), false);
});

test('SPOT-6555: a signed-out listing shows its reason and keeps the imported rows', () => {
	const html = renderPanel({
		initialAccount: {
			connected: false,
			reason: 'not_signed_in',
			message: 'Sign in to Spotify.',
			playlists: []
		},
		playlists: [playlist()]
	});
	// SSR is not on the engine's machine, so it points there instead of Connect.
	assert.match(html, /Sign in to list all your Spotify playlists\./);
	assert.equal(
		html.includes('playlists</span'),
		false,
		'a signed-out listing must not name an account'
	);
	assert.equal(html.includes('aria-label="Imported into Open DJ"'), true);
	assert.match(html, /1 \/ 1/);
});
