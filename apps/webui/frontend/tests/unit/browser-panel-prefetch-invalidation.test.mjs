// requirement: PERF-UI-05
// BrowserPanel must never join a playlist prefetch taken before its own write
// (review of 674c1318, P1). These read the component source because the
// wiring, not the module, is what the defect was in.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const panel = readFileSync(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url),
	'utf8'
);
const tree = readFileSync(
	new URL('../../src/lib/components/rb/browser/PlaylistTree.svelte', import.meta.url),
	'utf8'
);

function body(name) {
	const start = panel.indexOf(`async function ${name}(`);
	assert.ok(start >= 0, `${name} not found in BrowserPanel.svelte`);
	const next = panel.indexOf('\n\tasync function ', start + 1);
	const nextSync = panel.indexOf('\n\tfunction ', start + 1);
	const end = Math.min(...[next, nextSync].filter((i) => i > 0));
	return panel.slice(start, end);
}

test('_loadPane invalidates before beginLoad when reloading the playlist it already shows', () => {
	const src = body('_loadPane');
	const guard = src.indexOf(
		'if (p.playlist_id === node.playlist_id) invalidatePlaylistFirstPage(node.playlist_id);'
	);
	const begin = src.indexOf('p.beginLoad(');
	assert.ok(guard >= 0, 'same-playlist reload guard missing');
	assert.ok(begin > guard, 'the guard must run before beginLoad overwrites playlist_id');
});

test('change events and resync drop every prefetch', () => {
	for (const hook of ["subscribeKind('tracks'", "subscribeKind('playlists'", 'subscribeResync(']) {
		const at = panel.indexOf(hook);
		assert.ok(at >= 0, `${hook} subscription missing`);
		const handler = panel.slice(at, panel.indexOf('});', at));
		assert.ok(handler.includes('invalidateAllPlaylistFirstPages()'), `${hook} does not invalidate`);
	}
});

test('drop and add-to-playlist writes invalidate the written playlists', () => {
	const drop = panel.slice(panel.indexOf('await transferPlaylistTracks('));
	assert.ok(drop.slice(0, 400).includes('invalidatePlaylistFirstPage(srcId)'));
	assert.ok(body('addTracksToPlaylist').includes('invalidatePlaylistFirstPage(node.playlist_id)'));
	assert.match(
		panel,
		/await appendTracksToPlaylist\(playlistId, stableIds\);\n\t+\}\n\t+invalidatePlaylistFirstPage\(playlistId\);/
	);
});

test('hover prefetch only fires for real playlists', () => {
	const hovers = tree.match(/onpointer(enter|down)=\{[^\n]*\}/g) ?? [];
	assert.equal(hovers.length, 2);
	for (const h of hovers) assert.ok(h.includes("node.kind === 'playlist'"), h);
});
