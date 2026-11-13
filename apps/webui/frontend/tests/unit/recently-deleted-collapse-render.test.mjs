/**
 * LIBUX-53: Recently deleted gates its subtree on `open` with the shared
 * collapse wrapper pair (SSR proves markup; in:collapse is source-regex only).
 */
// requirement: LIBUX-53
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';
import { bundleRecentlyDeletedFolderViewClient } from './mount-svelte-client.mjs';
import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as RecentlyDeletedFolderView } from '$lib/components/rb/browser/RecentlyDeletedFolderView.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let ssr;
let client;
let teardownDom;

before(async () => {
	teardownDom = installQualityBadgeDom();
	ssr = await loadSvelteSsrModule(ENTRY);
	client = await bundleRecentlyDeletedFolderViewClient();
});

after(() => {
	teardownDom?.();
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

function findByTestId(root, testId) {
	if (root.getAttribute?.('data-testid') === testId) return root;
	for (const child of root.childNodes ?? []) {
		const hit = findByTestId(child, testId);
		if (hit) return hit;
	}
	return null;
}

function mountHarness(props) {
	const { mount, flushSync } = client;
	const target = document.createElement('div');
	document.body.appendChild(target);
	const instance = mount(client.RecentlyDeletedFolderHarness, {
		target,
		props
	});
	flushSync();
	return {
		target,
		instance,
		done: () => {
			client.unmount(instance);
			target.remove();
		}
	};
}

const refreshedRows = [
	deleted('pl-2', 'After Refresh A', 11),
	deleted('pl-4', 'After Refresh B', 3)
];

// [if] expanded recently deleted gets new rows [then] the collapse node is unchanged, [else stop].
test('#13326: expanded Recently deleted keeps the same collapse element after rows refresh', () => {
	const h = mountHarness({ initialOpen: true, initialRows: sampleRows, initialError: null });
	try {
		const collapseEl = h.target.querySelector('.collapse');
		assert.ok(collapseEl, 'collapse wrapper');
		assert.equal(findByTestId(h.target, 'playlist-deleted-row') !== null, true);
		h.instance.setRows(refreshedRows);
		client.flushSync();
		assert.equal(h.target.querySelector('.collapse'), collapseEl);
		assert.equal(findByTestId(h.target, 'playlist-deleted-row') !== null, true);
		assert.ok(h.target.textContent.includes('After Refresh A'));
		assert.ok(!h.target.textContent.includes('Old Warmup'));
	} finally {
		h.done();
	}
});

// [if] collapsed recently deleted gets new rows [then] the collapse subtree stays absent, [else stop].
test('#13326: collapsed Recently deleted stays closed after rows refresh', () => {
	const h = mountHarness({ initialOpen: false, initialRows: sampleRows, initialError: null });
	try {
		assert.equal(h.target.querySelector('.collapse'), null);
		assert.ok(findByTestId(h.target, 'playlist-recently-deleted'));
		h.instance.setRows(refreshedRows);
		client.flushSync();
		assert.equal(h.target.querySelector('.collapse'), null);
		assert.equal(findByTestId(h.target, 'playlist-deleted-row'), null);
		assert.ok(findByTestId(h.target, 'playlist-recently-deleted'));
	} finally {
		h.done();
	}
});
