/**
 * requirement: OFFLOAD-8598f
 *
 * Cloud offload in the browser (spec e8da14b8): row treatment, load
 * routing, the right-click items and the progress line. The modules under
 * test run for real through Vite's SSR loader; nothing is stubbed.
 *
 * - [if] a row is offloaded [then] it renders muted with the cloud view, never as a broken link, [else stop].
 * - [if] a row is offloaded [then] the menu offers Download for 24h and Re-download permanently, and Offload is disabled with a reason, [else stop].
 * - [if] an operation reports bytes and counts [then] the line reads them as the engine sent them, [else stop].
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
let vite;
let cloud;
let wire;
let registry;
let api;
let drag;
let drop;

test.before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: { alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') } }
	});
	cloud = await vite.ssrLoadModule('/src/lib/components/rb/browser/track-cloud-state.ts');
	wire = await vite.ssrLoadModule('/src/lib/components/rb/browser/browser-row-wire.ts');
	registry = await vite.ssrLoadModule('/src/lib/context-menu/registry.ts');
	api = await vite.ssrLoadModule('/src/lib/offload/api-offload.ts');
	drag = await vite.ssrLoadModule('/src/lib/rb/track-drag-refusal.ts');
	drop = await vite.ssrLoadModule('/src/lib/rb/deck-track-drop.ts');
});

test.after(async () => {
	await vite?.close();
});

const offloadedRow = {
	stable_id: 'a'.repeat(40),
	file_exists: false,
	file_availability: 'absent',
	is_streaming: false,
	offload: { state: 'offloaded' }
};

const baseCloudInput = {
	fileExists: false,
	isStreaming: false,
	hasRemoteCopy: true,
	spotifyPending: false,
	transfer: null
};

test('an offloaded row is the muted cloud view, never a broken link (veto 2)', () => {
	const view = cloud.trackCloudView({
		...baseCloudInput,
		offload: { state: 'offloaded', expires_at: null }
	});
	assert.equal(view.kind, 'offloaded');
	assert.equal(view.overlay, 'none');
	assert.doesNotMatch(view.title, /broken link|missing/);
	assert.match(view.title, /Loading it onto a deck downloads it first/);
	assert.equal(wire.rowRendersUnavailable(offloadedRow), false);
	assert.equal(
		wire.libraryRowHoverTitle(offloadedRow),
		'Offloaded to the cloud. Loading it onto a deck downloads it first.'
	);
});

test('the same missing file without an offload record is still a broken link', () => {
	const row = { ...offloadedRow, offload: null };
	assert.equal(wire.rowRendersUnavailable(row), true);
	assert.match(wire.libraryAudioLoadRefusal(row), /broken link/);
});

test('a temporary restore says when it goes back to the cloud', () => {
	const view = cloud.trackCloudView({
		...baseCloudInput,
		fileExists: true,
		offload: { state: 'temporary', expires_at: '2026-11-05T12:00:00.000000Z' }
	});
	assert.equal(view.kind, 'temporary');
	assert.match(view.title, /^Downloaded for 24h; it goes back to the cloud at /);
});

test('only an offloaded row is routed through a download before it loads', () => {
	assert.equal(wire.rowIsOffloaded(offloadedRow), true);
	assert.equal(wire.rowIsOffloaded({ offload: { state: 'restoring' } }), true);
	assert.equal(wire.rowIsOffloaded({ offload: { state: 'temporary' } }), false);
	assert.equal(wire.rowIsOffloaded({ offload: null }), false);
	assert.equal(wire.libraryAudioLoadRefusal(offloadedRow), wire.OFFLOADED_REFUSAL);
});

function trackMenu(audio, selection = 'one') {
	const items = registry.menuFor(
		{ kind: 'track', count: selection === 'one' ? 1 : 3, rows: selection === 'one' ? 1 : 3 },
		{ selection, source: 'collection', audio, stems: 'none', workspace: 'performance' }
	);
	return Object.fromEntries(items.map((item) => [item.id, item]));
}

test('menu: an offloaded row offers both downloads and refuses a second offload', () => {
	assert.equal(registry.trackAudioOf(offloadedRow), 'offloaded');
	const menu = trackMenu('offloaded');
	assert.equal(menu['offload-restore-24h'].enabled, true);
	assert.equal(menu['offload-restore-permanent'].enabled, true);
	assert.equal(menu.offload.enabled, false);
	assert.match(menu.offload.disabledReason, /already offloaded/);
	assert.equal(menu['load-1'].enabled, true, 'a deck load downloads it first');
	assert.equal(menu.finder.enabled, false, 'there is no local file to reveal');
});

test('menu: a local row offers Offload and explains why the downloads are off', () => {
	const menu = trackMenu('present');
	assert.equal(menu.offload.enabled, true);
	assert.equal(menu.offload.label, 'Offload to cloud');
	assert.equal(menu['offload-restore-24h'].enabled, false);
	assert.match(menu['offload-restore-24h'].disabledReason, /already on this machine/);
});

test('menu: a temporary row can be kept for good or offloaded again', () => {
	assert.equal(
		registry.trackAudioOf({ ...offloadedRow, file_exists: true, offload: { state: 'temporary' } }),
		'temporary'
	);
	const menu = trackMenu('temporary');
	assert.equal(menu.offload.enabled, true);
	assert.equal(menu['offload-restore-permanent'].enabled, true);
});

test('menu: a multi-row selection offloads every row and names the count', () => {
	const menu = trackMenu('present', 'many');
	assert.equal(menu.offload.label, 'Offload 3 tracks to cloud');
	assert.equal(menu['offload-restore-24h'].enabled, true);
});

test('menu: a playlist offers offload and both downloads', () => {
	const ids = registry
		.menuFor({ kind: 'playlist' }, { forbidDuplicates: false })
		.map((item) => item.id);
	for (const id of [
		'offload-playlist',
		'offload-playlist-restore-24h',
		'offload-playlist-restore-permanent'
	]) {
		assert.ok(ids.includes(id), `${id} missing from the playlist menu`);
	}
});

test('progress line reads the engine numbers verbatim', () => {
	const op = {
		op_id: 'x',
		kind: 'offload',
		mode: null,
		playlist_id: 'p',
		state: 'running',
		stable_ids: ['a', 'b'],
		tracks_total: 12,
		tracks_done: 3,
		bytes_total: 4 * 1024 ** 3,
		bytes_done: 1024 ** 3,
		bytes_freed: 0,
		current: 'a',
		results: {},
		error: null,
		dry_run: false
	};
	assert.equal(api.operationLabel(op), 'Offloading: 3 of 12 tracks, 1.00 GB of 4.00 GB');
	assert.equal(api.operationPercent(op), 25);
	assert.equal(
		api.operationPercent({ ...op, bytes_total: 0 }),
		null,
		'an unknown total is not a guessed percent'
	);
	assert.deepEqual(
		api.operationErrors({
			...op,
			results: {
				['b'.repeat(40)]: { state: null, note: null, error: 'TRACK_ON_DECK', message: 'on a deck' }
			}
		}),
		['bbbbbbbb: on a deck']
	);
});

test('an offloaded row drags onto a deck; the same missing row without offload does not', () => {
	assert.equal(drag.trackDragRefusal(offloadedRow), null);
	assert.match(drag.trackDragRefusal({ ...offloadedRow, offload: null }), /broken link/);
	const meta = drop.draggedDeckRows([offloadedRow.stable_id], [offloadedRow], () => 0);
	assert.deepEqual(meta[offloadedRow.stable_id].offload, { state: 'offloaded' });
	assert.equal(
		drag.trackDragRefusal(meta[offloadedRow.stable_id]),
		null,
		'the drop-side guard agrees'
	);
});
