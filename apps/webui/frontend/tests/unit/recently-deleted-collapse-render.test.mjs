/**
 * LIBUX-53: Recently deleted gates its subtree on `open` with the shared
 * collapse wrapper pair (SSR proves markup; in:collapse is source-regex only).
 */
// requirement: LIBUX-53
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as RecentlyDeletedFolderView } from '$lib/components/rb/browser/RecentlyDeletedFolderView.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let ssr;

before(async () => {
	ssr = await loadSvelteSsrModule(ENTRY);
});

after(() => {
	delete globalThis.window;
});

const deleted = (playlist_id, name, track_count) => ({
	playlist_id,
	name,
	track_count,
	deleted_at: '2026-10-12T21:04:00Z',
	updated_at: '2026-10-12T21:04:00Z',
	vendor: 'rekordbox',
	vendor_pl_id: playlist_id
});

const sampleRows = [deleted('pl-7', 'Old Warmup', 32), deleted('pl-9', 'Test Crate', 4)];

const noop = () => {};

/** SSR hydration markers (`<!--[0-->`, `<!---->`) are not markup; drop them before matching. */
const stripMarkers = (html) => html.replace(/<!--[\s\S]*?-->/g, '');

function renderView(props) {
	return stripMarkers(
		ssr.render(ssr.RecentlyDeletedFolderView, {
			props: { ontoggle: noop, onrestore: noop, ...props }
		}).body
	);
}

const hasClass = (html, name) => new RegExp(`class="${name}(?:\\s|")`).test(html);

test('LIBUX-53: open true renders collapse wrappers and deleted rows', () => {
	const html = renderView({ open: true, rows: sampleRows, error: null });
	assert.ok(hasClass(html, 'collapse'));
	assert.ok(hasClass(html, 'collapse-inner'));
	assert.match(html, /data-testid="playlist-deleted-row"/);
	assert.match(html, /data-testid="playlist-recently-deleted"/);
});

test('LIBUX-53: open false keeps the folder header but hides the subtree', () => {
	const html = renderView({ open: false, rows: sampleRows, error: null });
	assert.match(html, /data-testid="playlist-recently-deleted"/);
	assert.ok(!hasClass(html, 'collapse'));
	assert.ok(!hasClass(html, 'collapse-inner'));
	assert.doesNotMatch(html, /data-testid="playlist-deleted-row"/);
});

test('LIBUX-53: open true with error shows collapse wrappers and error copy', () => {
	const html = renderView({ open: true, rows: null, error: 'playlists_unavailable' });
	assert.ok(hasClass(html, 'collapse'));
	assert.ok(hasClass(html, 'collapse-inner'));
	assert.match(html, /deleted playlists unavailable/);
});

test('LIBUX-53: open true with empty list shows collapse wrappers and empty testid', () => {
	const html = renderView({ open: true, rows: [], error: null });
	assert.ok(hasClass(html, 'collapse'));
	assert.ok(hasClass(html, 'collapse-inner'));
	assert.match(html, /data-testid="playlist-recently-deleted-empty"/);
});
