// requirement: LIBUX-14
// [if] the rekordbox playlist tree is loading, erroring, or genuinely empty [then] each state renders distinct copy, not the same blank space
// [if] a playlist is mostly broken (dimmed) [then] hovering the row shows a tooltip that names the 30% playable threshold
// [if] Hide broken links hides one or more playlists [then] the tree shows a count of how many are hidden
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as States } from '$lib/components/rb/browser/PlaylistFolderStates.svelte';",
	"export { default as Hidden } from '$lib/components/rb/browser/PlaylistHiddenBrokenNotice.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

const treeSource = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/PlaylistTree.svelte', import.meta.url)),
	'utf8'
);

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function props(overrides = {}) {
	return {
		nodeCount: 0,
		playlistsLoading: false,
		playlistsError: null,
		hiddenBrokenPlaylistCount: 0,
		...overrides
	};
}

function renderStates(overrides = {}) {
	return mod.render(mod.States, { props: props(overrides) }).body;
}

function renderHidden(count) {
	return mod.render(mod.Hidden, { props: { hiddenBrokenPlaylistCount: count } }).body;
}

test('loading: shows playlists-loading and not empty/error/hidden copy', () => {
	const html = renderStates({ playlistsLoading: true });
	assert.match(html, /data-testid="playlists-loading"/);
	assert.match(html, /Loading playlists\.\.\./);
	assert.equal(html.includes('no playlists yet'), false);
	assert.equal(html.includes('Playlist load failed'), false);
	assert.equal(html.includes('hidden by Broken filter'), false);
});

test('error: shows playlists-error with message and not empty/loading copy', () => {
	const html = renderStates({ playlistsError: 'boom' });
	assert.match(html, /data-testid="playlists-error"/);
	assert.match(html, /Playlist load failed/);
	assert.match(html, /title="boom"/);
	assert.equal(html.includes('no playlists yet'), false);
	assert.equal(html.includes('Loading playlists'), false);
});

test('genuine empty: shows playlists-empty and not loading/error/hidden copy', () => {
	const html = renderStates();
	assert.match(html, /data-testid="playlists-empty"/);
	assert.match(html, /no playlists yet/);
	assert.equal(html.includes('Loading playlists'), false);
	assert.equal(html.includes('Playlist load failed'), false);
	assert.equal(html.includes('hidden by Broken filter'), false);
});

test('mostly-broken row: broken class and threshold tooltip on PlaylistTree rows', () => {
	assert.match(treeSource, /class:broken=\{node\.mostly_broken\}/);
	assert.match(
		treeSource,
		/title=\{node\.mostly_broken \? _mostlyBrokenTitle\(node\) : undefined\}/
	);
	assert.match(treeSource, /Fewer than 30% of tracks in this playlist are playable/);
});

test('all hidden: shows hidden count and not genuine-empty copy', () => {
	const html = renderHidden(3);
	assert.match(html, /data-testid="playlists-hidden-broken"/);
	assert.match(html, /3 playlists hidden by Broken filter/);
	const prefix = renderStates({ hiddenBrokenPlaylistCount: 3 });
	assert.equal(prefix.includes('no playlists yet'), false);
});

test('hidden count with visible nodes: singular hidden notice', () => {
	const html = renderHidden(1);
	assert.match(html, /1 playlist hidden by Broken filter/);
});

test('loading wins over stale hidden count', () => {
	const html = renderStates({ playlistsLoading: true, hiddenBrokenPlaylistCount: 2 });
	assert.match(html, /data-testid="playlists-loading"/);
	assert.equal(html.includes('hidden by Broken filter'), false);
});
