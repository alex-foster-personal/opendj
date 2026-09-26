// requirement: LIBUX-14
// [if] the rekordbox playlist tree is loading, erroring, or genuinely empty [then] each state renders distinct copy, not the same blank space
// [if] a playlist is mostly broken (dimmed) [then] hovering the row shows a tooltip that names the min playable-track threshold
// [if] Hide broken links hides one or more playlists [then] the tree shows a count of how many are hidden
// requirement: LIBUX-15
// [if] a user has zero playlists and playlist creation is wired [then] the empty state renders a labeled, clickable call to action instead of inert text
// [if] a user has zero playlists and playlist creation is NOT wired [then] the empty state falls back to the plain inert "no playlists yet" copy
// [if] a playlist was just created via create-then-rename and is still empty [then] PlaylistTree renders a hint row naming the next step (dragging tracks/a folder in)
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
const renameSource = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/tree-playlist-rename.svelte.ts', import.meta.url)),
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

test('genuine empty with create wired: renders a labeled CTA instead of inert text', () => {
	const html = mod.render(mod.States, { props: { ...props(), oncreate: () => {} } }).body;
	assert.match(html, /data-testid="playlists-empty-cta"/);
	assert.match(html, /Create your first playlist/);
	assert.equal(html.includes('data-testid="playlists-empty"'), false);
});

test('empty-state CTA reuses the same create-then-rename flow as the header + button', () => {
	assert.match(
		treeSource,
		/oncreate=\{oncreateplaylist \? \(\) => void rename\.createAndRename\(\) : undefined\}/,
		'the empty-state CTA must call the same rename.createAndRename() flow, not a duplicate creation path'
	);
});

test('a just-created empty playlist shows a next-step hint until it gets a track', () => {
	assert.match(
		treeSource,
		/\{#if rename\.createdId === node\.playlist_id && node\.track_count === 0\}/,
		'the hint must be scoped to the just-created playlist and clear itself once it is no longer empty'
	);
	assert.match(treeSource, /data-testid="playlist-new-hint"/);
	assert.match(treeSource, /Drag tracks or a folder here to add music/);
});

test('createdId survives past the rename commit, unlike the transient editingId', () => {
	assert.match(renameSource, /createdId: string \| null = \$state\(null\);/);
	assert.match(
		renameSource,
		/this\.pendingId = null;\s*this\.createdId = id;\s*void this\.begin\(node\);/,
		'createdId must be set alongside checkPending resolving a pending create, before rename begins'
	);
});

test('mostly-broken row: broken class and threshold tooltip on PlaylistTree rows', () => {
	assert.match(treeSource, /class:broken=\{node\.mostly_broken\}/);
	assert.match(
		treeSource,
		/title=\{node\.mostly_broken \? formatMostlyBrokenTooltip\(\) : undefined\}/
	);
	assert.match(treeSource, /formatMostlyBrokenTooltip\(\)/);
	const policy = readFileSync(
		new URL('../../src/lib/rb/runtime-policy.svelte.ts', import.meta.url),
		'utf8'
	);
	assert.match(policy, /Fewer than \$\{n\} tracks in this playlist are playable/);
	assert.match(
		policy,
		/runtimePolicy\.hide_broken_playlist_min_available_tracks/,
		'tooltip must use the server-hydrated min playable-track count (shipped default 4)'
	);
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
