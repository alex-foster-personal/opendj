/**
 * ARTTHUMB-14783: mounted /playlists grid refetches on playlist invalidation.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const page = readFileSync(
	new URL('../../src/routes/playlists/+page.svelte', import.meta.url),
	'utf8'
);

function onMountBody() {
	const start = page.indexOf('onMount(() => {');
	assert.ok(start >= 0, 'onMount hook missing');
	const end = page.indexOf('\n\t});', start);
	assert.ok(end > start, 'onMount hook not closed');
	return page.slice(start, end);
}

test('[if] playlist membership bumps revision [then] mounted grid refetches via bus, [else stop]', () => {
	assert.ok(
		page.includes("subscribeKind('playlists'"),
		'playlists invalidation subscription missing'
	);
	assert.ok(page.includes('subscribeResync('), 'resync subscription missing');
	assert.ok(page.includes('coalesce('), 'coalesced refresh missing');
	assert.ok(page.includes('listPlaylists'), 'listPlaylists refetch missing');

	const mount = onMountBody();
	assert.ok(mount.includes('requestRefresh'), 'onMount must trigger coalesced refresh');
	const afterSub = mount.slice(mount.indexOf("subscribeKind('playlists'"));
	assert.ok(afterSub.includes('requestRefresh'), 'playlists handler must call refresh');

	const cleanup = mount.slice(mount.indexOf('return () => {'));
	assert.ok(cleanup.includes('unkind()'), 'playlists unsubscribe missing');
	assert.ok(cleanup.includes('unresync()'), 'resync unsubscribe missing');
});

// The cards moved into PlaylistsGridBrowser.svelte, which the page renders with its refreshed rows.
const grid = readFileSync(
	new URL('../../src/lib/components/rb/browser/PlaylistsGridBrowser.svelte', import.meta.url),
	'utf8'
);

test('playlist cards still bind thumbnail src with updated_at revision', () => {
	assert.match(page, /<PlaylistsGridBrowser [^>]*\{playlists\}/);
	assert.match(grid, /playlistThumbnailUrl\(pl\.playlist_id, 232, pl\.updated_at\)/);
});
