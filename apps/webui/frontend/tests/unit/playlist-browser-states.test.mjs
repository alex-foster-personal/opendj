// requirement: LIBM-14435
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as Browser } from '$lib/components/rb/browser/PlaylistsGridBrowser.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

/** @type {{ Browser: import('svelte').Component; render: typeof import('svelte/server').render }} */
let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

/** @param {Record<string, unknown>} overrides */
function renderBrowser(overrides = {}) {
	const props = {
		artThumbnailsOn: true,
		playlists: null,
		loadError: null,
		...overrides
	};
	return mod.render(mod.Browser, { props }).body;
}

test('[if] playlists are still loading [then] playlist-browser-loading renders and not empty/error/success', () => {
	const html = renderBrowser();
	assert.match(html, /data-testid="playlist-browser-loading"/);
	assert.match(html, /Loading playlists\.\.\./);
	assert.doesNotMatch(html, /data-testid="playlist-browser-empty"/);
	assert.doesNotMatch(html, /data-testid="playlist-browser-error"/);
	assert.doesNotMatch(html, /data-testid="playlist-browser-success"/);
});

test('[if] the playlist list is empty [then] playlist-browser-empty renders and not loading/error/success', () => {
	const html = renderBrowser({ playlists: [] });
	assert.match(html, /data-testid="playlist-browser-empty"/);
	assert.match(html, /No playlists yet\./);
	assert.doesNotMatch(html, /data-testid="playlist-browser-loading"/);
	assert.doesNotMatch(html, /data-testid="playlist-browser-error"/);
	assert.doesNotMatch(html, /data-testid="playlist-browser-success"/);
});

test('[if] the playlist request has a recoverable failure [then] playlist-browser-error names the reason', () => {
	const html = renderBrowser({ loadError: 'HTTP 503' });
	assert.match(html, /data-testid="playlist-browser-error"/);
	assert.match(html, /Could not load playlists: HTTP 503/);
	assert.match(html, /role="alert"/);
	assert.doesNotMatch(html, /data-testid="playlist-browser-loading"/);
	assert.doesNotMatch(html, /data-testid="playlist-browser-empty"/);
	assert.doesNotMatch(html, /data-testid="playlist-browser-success"/);
});

test('[if] the playlist request succeeds [then] playlist-browser-success lists cards', () => {
	const html = renderBrowser({
		playlists: [
			{
				playlist_id: 'pl-1',
				name: 'Warm Up',
				track_count: 12,
				updated_at: 'rev-1'
			}
		]
	});
	assert.match(html, /data-testid="playlist-browser-success"/);
	assert.match(html, /data-testid="playlist-card"/);
	assert.match(html, /Warm Up/);
	assert.match(html, /12 tracks/);
	assert.doesNotMatch(html, /data-testid="playlist-browser-loading"/);
	assert.doesNotMatch(html, /data-testid="playlist-browser-empty"/);
	assert.doesNotMatch(html, /data-testid="playlist-browser-error"/);
});
