/**
 * ARTTHUMB-8728c / ARTTHUMB-8728d: the playlist art view option and its tiles.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

let ssr;

before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as PlaylistTreeView } from '$lib/components/rb/browser/PlaylistTreeView.svelte';",
			"export { default as meta } from '$lib/components/rb/browser/PlaylistTreeView.stories';",
			"export { default as PlaylistsGridBrowser } from '$lib/components/rb/browser/PlaylistsGridBrowser.svelte';",
			"export * as art from '$lib/rb/library-sidebar-art.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

function body(sidebarart) {
	return ssr.render(ssr.PlaylistTreeView, { props: { ...ssr.meta.args, sidebarart } }).body;
}

/** Minimal playlist row for SSR props (mirrors stories' playlist() helper). */
function playlistNode(playlist_id, name, track_count, over = {}) {
	return {
		playlist_id,
		name,
		track_count,
		broken_count: 0,
		kind: 'playlist',
		children: [],
		...over
	};
}

test('ARTTHUMB-8728c tiles show only with the option on, sized 48 px with a hover title', () => {
	const on = body(true);
	const tiles = on.match(/<img[^>]*data-testid="playlist-art-tile"[^>]*>/g) ?? [];
	assert.ok(tiles.length > 0, 'option on renders a tile per playlist row');
	for (const tile of tiles) {
		assert.match(tile, /src="[^"]*\/api\/v1\/playlists\/[^"]+\/thumbnail\?size=64/);
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
	assert.match(
		playlistThumbnailUrl('a b/c', 232),
		/\/api\/v1\/playlists\/a%20b%2Fc\/thumbnail\?size=232$/
	);
});

test('playlistThumbnailUrl optional rev: encodes timestamp and omits when empty or absent', () => {
	const { playlistThumbnailUrl } = ssr.art;
	const stamp = '2026-11-06T12:00:00Z';
	const withRev = playlistThumbnailUrl('pid', 232, stamp);
	assert.match(withRev, /[?&]size=232/);
	assert.ok(withRev.includes(`&v=${encodeURIComponent(stamp)}`));
	assert.doesNotMatch(withRev, /\/artwork/);

	assert.match(playlistThumbnailUrl('pid', 232, ''), /\/thumbnail\?size=232$/);
	assert.doesNotMatch(playlistThumbnailUrl('pid', 232, ''), /&v=/);
	assert.match(playlistThumbnailUrl('pid', 232, null), /\/thumbnail\?size=232$/);
	assert.doesNotMatch(playlistThumbnailUrl('pid', 232, null), /&v=/);
	assert.match(playlistThumbnailUrl('pid', 232, undefined), /\/thumbnail\?size=232$/);
	assert.doesNotMatch(playlistThumbnailUrl('pid', 232, undefined), /&v=/);

	const a = playlistThumbnailUrl('same-id', 64, '2026-01-01T00:00:00Z');
	const b = playlistThumbnailUrl('same-id', 64, '2026-01-02T00:00:00Z');
	assert.notEqual(a, b);
});

test('ARTTHUMB-8728c tile src includes v= when node.updated_at is set and changes with revision', () => {
	const rev1 = '2026-11-06T10:00:00Z';
	const rev2 = '2026-11-06T11:00:00Z';
	const nodes = [playlistNode('pl-rev', 'Rev Test', 3, { updated_at: rev1 })];
	const html1 = ssr.render(ssr.PlaylistTreeView, {
		props: { ...ssr.meta.args, sidebarart: true, nodes }
	}).body;
	const m1 = html1.match(/data-testid="playlist-art-tile"[^>]*src="([^"]+)"/);
	assert.ok(m1, 'expected one art tile');
	const src1 = m1[1];
	assert.match(src1, /thumbnail\?size=64/);
	assert.ok(src1.includes(`v=${encodeURIComponent(rev1)}`));

	const html2 = ssr.render(ssr.PlaylistTreeView, {
		props: {
			...ssr.meta.args,
			sidebarart: true,
			nodes: [playlistNode('pl-rev', 'Rev Test', 3, { updated_at: rev2 })]
		}
	}).body;
	const m2 = html2.match(/data-testid="playlist-art-tile"[^>]*src="([^"]+)"/);
	assert.ok(m2);
	assert.notEqual(m2[1], src1);
	assert.ok(m2[1].includes(`v=${encodeURIComponent(rev2)}`));
});

// A tree node with no updated_at keeps today's two-arg URL: no v= revision param at all.
test('ARTTHUMB-8728c tile src omits v= when node.updated_at is absent or empty', () => {
	for (const over of [{}, { updated_at: '' }]) {
		const nodes = [playlistNode('pl-norev', 'No Rev', 2, over)];
		const html = ssr.render(ssr.PlaylistTreeView, {
			props: { ...ssr.meta.args, sidebarart: true, nodes }
		}).body;
		const m = html.match(/data-testid="playlist-art-tile"[^>]*src="([^"]+)"/);
		assert.ok(m, 'expected one art tile');
		assert.match(m[1], /\/api\/v1\/playlists\/pl-norev\/thumbnail\?size=64$/);
		assert.doesNotMatch(m[1], /[?&]v=/);
	}
});

// Source pin: both thumbnail consumers pass the PlaylistSummary.updated_at revision into the URL helper.
// Since PR #14435 the /playlists card markup lives in PlaylistsGridBrowser.svelte; the route page
// only composes it, so the pin reads the component and checks the page still hands it the rows.
test('ARTTHUMB-8728c/d browser tree and /playlists grid pass the membership revision to playlistThumbnailUrl', () => {
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	const tree = readFileSync(`${SRC}/lib/components/rb/browser/PlaylistTreeView.svelte`, 'utf8');
	const grid = readFileSync(`${SRC}/lib/components/rb/browser/PlaylistsGridBrowser.svelte`, 'utf8');
	const page = readFileSync(`${SRC}/routes/playlists/+page.svelte`, 'utf8');
	assert.match(
		panel,
		/\(p\): PlaylistNode => \(\{[^}]*playlist_id: p\.playlist_id,[^}]*updated_at: p\.updated_at,/,
		'BrowserPanel must copy PlaylistSummary.updated_at onto library playlist nodes'
	);
	assert.match(
		tree,
		/playlistThumbnailUrl\(node\.playlist_id, 64, node\.updated_at\)/,
		'the sidebar tile must pass node.updated_at as the revision'
	);
	assert.match(
		grid,
		/playlistThumbnailUrl\(pl\.playlist_id, 232, pl\.updated_at\)/,
		'the /playlists card must pass pl.updated_at as the revision'
	);
	assert.match(
		page,
		/<PlaylistsGridBrowser[^>]*\{playlists\}/,
		'the /playlists route must render its PlaylistSummary rows through PlaylistsGridBrowser'
	);
	assert.doesNotMatch(
		page,
		/playlistThumbnailUrl\(/,
		'the route page must not build a second thumbnail URL of its own'
	);
	for (const [name, text] of [
		['PlaylistTreeView', tree],
		['playlists grid', grid]
	]) {
		const calls = text.match(/playlistThumbnailUrl\([^)]*\)/g) ?? [];
		assert.ok(calls.length > 0, `${name} builds a thumbnail URL`);
		for (const call of calls) {
			assert.match(call, /updated_at\)$/, `${name} call ${call} drops the membership revision`);
		}
	}
});

// Runtime proof for the grid half of the pin above: the rendered card carries v=<updated_at> and it moves with the revision.
test('ARTTHUMB-8728d rendered /playlists card src includes v= from updated_at and changes with revision', () => {
	const rev1 = '2026-11-06T10:00:00Z';
	const rev2 = '2026-11-06T11:00:00Z';
	const card = (updated_at) =>
		ssr.render(ssr.PlaylistsGridBrowser, {
			props: {
				artThumbnailsOn: true,
				loadError: null,
				playlists: [{ playlist_id: 'pl rev', name: 'Rev Grid', track_count: 4, updated_at }]
			}
		}).body;
	const src = (html) => {
		const m = html.match(/<a[^>]*data-testid="playlist-card"[^>]*>\s*<img[^>]*src="([^"]+)"/);
		assert.ok(m, 'expected one playlist card with an img');
		return m[1];
	};
	const src1 = src(card(rev1));
	assert.match(src1, /\/api\/v1\/playlists\/pl%20rev\/thumbnail\?size=232/);
	assert.ok(
		src1.includes(`&amp;v=${encodeURIComponent(rev1)}`),
		'SSR escapes & in attributes, so the rendered src carries &amp;v='
	);
	const src2 = src(card(rev2));
	assert.notEqual(src2, src1);
	assert.ok(src2.includes(`&amp;v=${encodeURIComponent(rev2)}`));
	assert.match(src(card('')), /\/thumbnail\?size=232$/, 'empty revision keeps the two-arg URL');
});
