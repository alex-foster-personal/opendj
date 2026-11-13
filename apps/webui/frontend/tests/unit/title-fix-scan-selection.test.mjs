// requirement: TITLEFIXSELECT-12587
// [if] selected library rows start a title-fix scan [then] the scans route gets exactly their stable_ids
// [if] a playlist-scoped scan is started [then] every page of the playlist reaches stable_ids
// [if] no rows are selected [then] nothing is posted and the refusal is toasted
// [if] the title-fix endpoint refuses a stable id [then] its message is toasted verbatim
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let scan;
let registry;
let commands;

before(async () => {
	scan = await loadTypeScriptModule('src/lib/rb/title-fix-scan.ts');
	registry = await loadTypeScriptModule('src/lib/context-menu/registry.ts');
	commands = await loadTypeScriptModule('src/lib/context-menu/commands.ts');
});

/** Fake deps recording posts and toasts. */
function fakeDeps(overrides = {}) {
	const posts = [];
	const toasts = [];
	return {
		posts,
		toasts,
		deps: {
			postScan: async (ids) => {
				posts.push(ids);
				return { scan_id: 's1' };
			},
			pushToast: (message, kind) => toasts.push({ message, kind }),
			fetchPlaylistPage: async () => ({ tracks: [], next_offset: null }),
			...overrides
		}
	};
}

test('selected rows post their deduplicated stable_ids', async () => {
	const { posts, toasts, deps } = fakeDeps();
	const result = await scan.startTitleFixScanForIds(['a', 'b', 'a'], deps);
	assert.deepEqual(result, { scan_id: 's1' });
	assert.deepEqual(posts, [['a', 'b']]);
	assert.equal(toasts[0].kind, 'info');
	assert.match(toasts[0].message, /2 tracks/);
});

test('a playlist scan pages the membership into one stable_ids scope', async () => {
	const pages = [];
	const { posts, deps } = fakeDeps({
		fetchPlaylistPage: async (playlistId, offset, limit) => {
			pages.push({ playlistId, offset, limit });
			return offset === 0
				? { tracks: [{ stable_id: 'x' }, { stable_id: 'y' }], next_offset: 2 }
				: { tracks: [{ stable_id: 'z' }], next_offset: null };
		}
	});
	await scan.startTitleFixScanForPlaylist('pl1', deps);
	assert.deepEqual(
		pages.map((p) => p.offset),
		[0, 2]
	);
	assert.ok(pages.every((p) => p.playlistId === 'pl1'));
	assert.deepEqual(posts, [['x', 'y', 'z']]);
});

test('no selected rows posts nothing and says why', async () => {
	const { posts, toasts, deps } = fakeDeps();
	assert.equal(await scan.startTitleFixScanForIds([], deps), null);
	assert.equal(await scan.startTitleFixScanForPlaylist('empty', deps), null);
	assert.deepEqual(posts, []);
	assert.equal(toasts.length, 2);
	assert.ok(toasts.every((t) => t.kind === 'warn'));
});

test('a selection over the server cap is refused before posting', () => {
	const ids = Array.from({ length: scan.TITLE_FIX_MAX_IDS + 1 }, (_, i) => `id${i}`);
	assert.ok('refused' in scan.titleFixScanScope(ids));
});

test('an endpoint refusal of a stable id is toasted verbatim', async () => {
	const { toasts, deps } = fakeDeps({
		postScan: async () => {
			throw new Error('unknown stable_id: ghost');
		}
	});
	assert.equal(await scan.startTitleFixScanForIds(['ghost'], deps), null);
	assert.deepEqual(toasts, [{ message: 'unknown stable_id: ghost', kind: 'error' }]);
});

test('track and playlist menus offer the scan on the title-fix scans route', () => {
	assert.equal(
		commands.describeCommand('title_fix.scan').includes('/api/v1/title-fix/scans'),
		true
	);
	const trackCtx = {
		selection: 'many',
		source: 'collection',
		audio: 'present',
		stems: 'none',
		tracklistHidden: 'shown',
		workspace: 'library'
	};
	const track = registry.menuFor({ kind: 'track', count: 3, rows: 3 }, trackCtx);
	const item = track.find((i) => i.id === 'title-fix-scan');
	assert.ok(item, 'track menu has title-fix-scan');
	assert.equal(item.command, 'title_fix.scan');
	const usb = registry.menuFor(
		{ kind: 'track', count: 1, rows: 1 },
		{ ...trackCtx, source: 'usb' }
	);
	assert.equal(
		usb.find((i) => i.id === 'title-fix-scan'),
		undefined
	);
	const playlist = registry.menuFor({ kind: 'playlist' }, { forbidDuplicates: false });
	assert.ok(playlist.find((i) => i.id === 'title-fix-scan'));
});
