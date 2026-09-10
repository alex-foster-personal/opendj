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
		name: 'Pleasure',
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
	assert.match(html, /aria-label="Pin Pleasure"/);
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

test('error branch is unchanged', () => {
	const html = renderPanel({ playlistsError: 'boom', playlists: [playlist()] });
	assert.match(html, /Spotify playlist load failed: boom/);
	assert.equal(html.includes('Filter Spotify playlists'), false);
});
