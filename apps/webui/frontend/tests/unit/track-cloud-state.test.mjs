/**
 * requirement: CHROME-02
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
let vite;
let cloud;

test.before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	cloud = await vite.ssrLoadModule('/src/lib/components/rb/browser/track-cloud-state.ts');
});

test.after(async () => {
	await vite.close();
});

test('streamingProviderFromPath parses provider URIs', () => {
	assert.equal(cloud.streamingProviderFromPath('spotify:track:abc'), 'spotify');
	assert.equal(cloud.streamingProviderFromPath('tidal:track:1'), 'tidal');
	assert.equal(cloud.streamingProviderFromPath('soundcloud:tracks:9'), 'soundcloud');
	assert.equal(cloud.streamingProviderFromPath('/music/foo.mp3'), 'unknown');
});

test('trackCloudView sets green tick on cloud-and-local', () => {
	const view = cloud.trackCloudView({
		fileExists: true,
		isStreaming: false,
		hasRemoteCopy: true,
		transfer: null
	});
	assert.equal(view.overlay, 'green-tick');
	assert.equal(view.provider, null);
});

test('trackCloudView sets blue tick on local-only', () => {
	const view = cloud.trackCloudView({
		fileExists: true,
		isStreaming: false,
		hasRemoteCopy: false,
		transfer: null
	});
	assert.equal(view.overlay, 'blue-tick');
});

test('streaming rows expose provider without tick overlay', () => {
	const view = cloud.trackCloudView({
		fileExists: false,
		isStreaming: true,
		hasRemoteCopy: false,
		folderPath: 'spotify:track:x',
		transfer: null
	});
	assert.equal(view.kind, 'streaming');
	assert.equal(view.provider, 'spotify');
	assert.equal(view.overlay, 'none');
});

test('CloudStatusIcon component exists for TrackTable', () => {
	const table = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
		'utf8'
	);
	const icon = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/CloudStatusIcon.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(table, /<CloudStatusIcon\b/);
	assert.match(icon, /tick-green|tick-blue|provider-icon/);
});

// Unmatched Spotify placeholders carry spotify_pending inline but have no
// rekordbox mapping, so rb_meta (and its folder_path) never arrives. The
// classifier must name Spotify from the inline fact, not fall through to the
// generic cloud.
test('an unmatched Spotify row (spotify_pending, no rb_meta path) classifies as Spotify streaming', () => {
	const view = cloud.trackCloudView({
		fileExists: false,
		isStreaming: false,
		hasRemoteCopy: false,
		spotifyPending: true,
		folderPath: null,
		transfer: null
	});
	assert.equal(view.kind, 'streaming');
	assert.equal(view.provider, 'spotify');
	assert.equal(view.overlay, 'none');
});

test('spotify_pending false leaves a pathless missing row on the cloud states (control)', () => {
	const view = cloud.trackCloudView({
		fileExists: false,
		isStreaming: false,
		hasRemoteCopy: false,
		spotifyPending: false,
		folderPath: null,
		transfer: null
	});
	assert.equal(view.kind, 'not-on-cloud');
	assert.equal(view.provider, null);
});

test('TrackTable feeds the inline spotify_pending fact to the cloud classifier', () => {
	const table = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
		'utf8'
	);
	const call = table.slice(table.indexOf('trackCloudView({'), table.indexOf('})}', table.indexOf('trackCloudView({')));
	assert.match(call, /spotifyPending: row\.spotify_pending === true,/);
});

test('the Spotify glyph draws three dark sound-wave arcs inside the green disk, not a bare dot', () => {
	const icon = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/CloudStatusIcon.svelte', import.meta.url)),
		'utf8'
	);
	const start = icon.indexOf("{#if view.provider === 'spotify'}");
	const block = icon.slice(start, icon.indexOf('{:else if', start));
	const circle = block.match(/<circle cx="(\d+(?:\.\d+)?)" cy="(\d+(?:\.\d+)?)" r="(\d+(?:\.\d+)?)" fill=\{providerColor\} \/>/);
	assert.ok(circle, 'the green disk must stay');
	const [cx, cy, r] = circle.slice(1).map(Number);
	assert.ok(cx - r >= 0 && cx + r <= 16, 'the disk stays inside the 16-unit viewBox');
	const arcs = [...block.matchAll(/<path class="spotify-arc" d="([^"]+)" fill="none" stroke="(#[0-9a-fA-F]{6})" stroke-width="([\d.]+)"/g)];
	assert.equal(arcs.length, 3, 'three sound-wave arcs');
	const widths = [];
	const bows = [];
	for (const [, d, stroke, width] of arcs) {
		assert.match(d, /^M[\d.]+ [\d.]+Q[\d.]+ [\d.]+ [\d.]+ [\d.]+$/, 'each arc is a curve, not a line');
		const lum = parseInt(stroke.slice(1, 3), 16) + parseInt(stroke.slice(3, 5), 16) + parseInt(stroke.slice(5, 7), 16);
		assert.ok(lum < 3 * 64, `arc stroke ${stroke} must be dark on the green disk`);
		const [x0, y0, qx, qy, x1, y1] = d.match(/[\d.]+/g).map(Number);
		for (const [x, y] of [[x0, y0], [x1, y1]]) {
			assert.ok(Math.hypot(x - cx, y - cy) < r, `arc point ${x},${y} lies inside the disk`);
		}
		// Control point above both ends: the arc bows upward like a sound wave.
		assert.ok(qy < Math.min(y0, y1));
		widths.push(Number(width));
		bows.push(x1 - x0);
	}
	assert.ok(widths[0] > widths[1] && widths[1] > widths[2], 'arcs thin toward the bottom');
	assert.ok(bows[0] > bows[1] && bows[1] > bows[2], 'arcs narrow toward the bottom');
	assert.match(icon, /<svg class="provider-icon" viewBox="0 0 16 16" width="12" height="12"/, 'icon size unchanged');
});

// Codex P2 on PR #3896: a streaming row with NO rekordbox mapping (djay-only,
// locally imported) never loads rb_meta, so rb_meta.folder_path is null and
// the provider was lost. The row carries streaming_provider inline now. The
// rows here are a CAPTURED real server response (manifest beside it), mapped
// through the real playlist-row mapper, not hand-built.
const FIXTURES = fileURLToPath(new URL('./fixtures/', import.meta.url));
const UNMAPPED_MANIFEST = fileURLToPath(
	new URL('./fixtures/playlist-unmapped-streaming-captured.manifest.json', import.meta.url)
);

async function unmappedRows() {
	const { verifiedFixtureText } = await import('./verified-fixture.mjs');
	const wire = await vite.ssrLoadModule('/src/lib/components/rb/browser/browser-row-wire.ts');
	const captured = JSON.parse(
		verifiedFixtureText(FIXTURES, UNMAPPED_MANIFEST, 'playlist-unmapped-streaming-captured.json')
	);
	return captured.map((w, i) => wire.rowFromPlaylistWire(w, i + 1));
}

/** Exactly what TrackTable passes for a row whose rb_meta never loads. */
function viewFor(row) {
	return cloud.trackCloudView({
		fileExists: row.file_exists === true,
		isStreaming: row.is_streaming ?? row.rb_meta?.is_streaming ?? false,
		hasRemoteCopy: row.has_remote_copy === true,
		spotifyPending: row.spotify_pending === true,
		provider: row.streaming_provider,
		folderPath: row.rb_meta?.folder_path ?? null,
		transfer: null
	});
}

test('unmapped Tidal and SoundCloud rows render their own provider, not the generic cloud', async () => {
	const rows = await unmappedRows();
	const byId = Object.fromEntries(rows.map((r) => [r.stable_id[0], r]));
	for (const [id, provider] of [['a', 'tidal'], ['b', 'soundcloud']]) {
		const row = byId[id];
		assert.equal(row.has_rb_mapping, false, 'the case under test is an unmapped row');
		assert.equal(row.rb_meta, null);
		const view = viewFor(row);
		assert.equal(view.kind, 'streaming');
		assert.equal(view.provider, provider);
	}
});

test('an unmapped https stream stays unknown and local rows stay off streaming (control)', async () => {
	const rows = await unmappedRows();
	const byId = Object.fromEntries(rows.map((r) => [r.stable_id[0], r]));
	assert.equal(viewFor(byId.c).provider, 'unknown');
	for (const id of ['d', 'e']) {
		const view = viewFor(byId[id]);
		assert.notEqual(view.kind, 'streaming', `row ${id} has present local audio`);
		assert.equal(view.provider, null);
	}
});

test('without an inline provider, a mapped row still reads it from rb_meta.folder_path (control)', () => {
	const view = cloud.trackCloudView({
		fileExists: false,
		isStreaming: true,
		hasRemoteCopy: false,
		spotifyPending: false,
		provider: null,
		folderPath: 'soundcloud:tracks:1',
		transfer: null
	});
	assert.equal(view.provider, 'soundcloud');
});

test('an inline provider never outranks spotify_pending (control)', () => {
	// A pending Spotify placeholder may carry a generic http(s) stream URI; the
	// pending fact is the stronger signal and still names Spotify.
	const view = cloud.trackCloudView({
		fileExists: false,
		isStreaming: true,
		hasRemoteCopy: false,
		spotifyPending: true,
		provider: 'unknown',
		folderPath: null,
		transfer: null
	});
	assert.equal(view.provider, 'spotify');
});

test('TrackTable feeds the inline streaming_provider to the cloud classifier', () => {
	const table = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
		'utf8'
	);
	const call = table.slice(table.indexOf('trackCloudView({'), table.indexOf('})}', table.indexOf('trackCloudView({')));
	assert.match(call, /provider: row\.streaming_provider,/);
});
