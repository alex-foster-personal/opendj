/**
 * PR #1656 review round 6 (src/lib/api.ts:451, chatgpt-codex-connector,
 * P2/BLOCKING) - the `forceInFlight: false` exception request-coalescer.ts
 * added for the bus's 'initial-connect' resync (see that file's docstring)
 * can leave `_restoreBootPane()` reasoning about a stale, falsely-empty
 * health snapshot, and nothing ever retries it.
 *
 * Sequence that reaches this: `_init()`'s boot health read is still in
 * flight when a track import lands elsewhere AND the WS opens for the first
 * time. The 'initial-connect' resync's `forceInFlight: false` leaves that
 * in-flight entry untouched (request-coalescer.test.mjs: "invalidate({
 * forceInFlight: false }) leaves an in-flight request untouched"), so
 * `_init()` reads the pre-import (zero-track) snapshot, `_restoreBootPane()`
 * correctly treats an apparently-empty library as "keep the explicit empty
 * state", and panes[0].playlist_id stays null. The SAME resync also triggers
 * `_libraryRefreshGate.request()` -> `_refreshLibraryRowsOnce()`, which reads
 * health again with `fresh: true` and gets the correct, post-import count -
 * but its per-pane loop explicitly `continue`s past any pane whose
 * playlist_id is still null ("a blank pane has nothing to refresh"), so
 * nothing ever calls `_restoreBootPane()` again. The pane stays blank
 * forever even though the fresh count it just read proves the library is not
 * empty.
 *
 * The node:test harness cannot mount a Svelte component, so this evaluates
 * BrowserPanel's real `_refreshLibraryRowsOnce` implementation directly from
 * its source (same technique as browser-panel-superseded-load-toast.test.mjs
 * and browser-panel-health-boot-retry.test.mjs), with its closure-captured
 * helpers supplied as factory arguments.
 *
 * Regression lines:
 * - if a fresh, corrected health count is read but panes[0] is left
 *   unclaimed with no retry, the boot pane is indistinguishable from a truly
 *   empty library until a manual reload -> broken
 * - if the retry fires even while a Spotify deep link is pending
 *   (spotifySelectedId !== null), it clobbers a deliberate
 *   spotifyPendingError state with an arbitrary local playlist -> broken
 * - if panes[0] is already claimed (a deep link, or a prior successful
 *   restore), the retry must be a no-op -> broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

/** Builds the real `_refreshLibraryRowsOnce`, evaluated straight from
 * BrowserPanel.svelte, with its closure-captured helpers supplied as factory
 * arguments so it can run outside the component. */
function makeRefreshLibraryRowsOnce({
	panes,
	source,
	spotifySelectedId,
	getHealth,
	restoreBootPane,
	trackCount
}) {
	const src = readFileSync(PANEL, 'utf8');
	const marker = '\tasync function _refreshLibraryRowsOnce(';
	const start = src.indexOf(marker);
	assert.ok(start >= 0, 'could not find BrowserPanel._refreshLibraryRowsOnce');
	const end = src.indexOf('\n\t/**\n\t * The only entry point for a background library refresh', start);
	assert.ok(end > start, 'could not isolate BrowserPanel._refreshLibraryRowsOnce');
	const functionSource = src
		.slice(start, end)
		.replace(
			'async function _refreshLibraryRowsOnce(): Promise<void> {',
			'async function _refreshLibraryRowsOnce() {'
		);
	assert.doesNotMatch(functionSource, /: Promise<void>/, 'TypeScript annotation survived stripping');
	const factory = Function(
		'panes',
		'source',
		'spotifySelectedId',
		'_loadIngestCoverage',
		'_loadReconcileSummary',
		'_refreshPlaylists',
		'getHealth',
		'_fetchAllRows',
		'_fetchPlaylistRows',
		'_restoreBootPane',
		`let allTracksCount = ${trackCount};\n${functionSource}\nreturn _refreshLibraryRowsOnce;`
	);
	return factory(
		panes,
		source,
		spotifySelectedId,
		async () => {},
		async () => {},
		async () => {},
		getHealth,
		async () => ({ rows: [], truncated: false, etag: 'x' }),
		async () => ({ rows: [], truncated: false, etag: 'x' }),
		restoreBootPane
	);
}

function blankPane() {
	return { playlist_id: null, loading: false, rows: [], selected_ids: [], selected_id: null };
}

test('a blank boot pane is retried once the fresh count proves the library is not empty', async () => {
	const panes = [blankPane()];
	let restoreCalls = 0;
	const refresh = makeRefreshLibraryRowsOnce({
		panes,
		source: 'local',
		spotifySelectedId: null,
		getHealth: async () => ({ health: { state_db: { tracks: 5 } } }),
		restoreBootPane: async () => {
			restoreCalls += 1;
		},
		trackCount: 0
	});

	await refresh();

	assert.equal(
		restoreCalls,
		1,
		'a still-unclaimed boot pane must be retried after a fresh health read corrects the count'
	);
});

test('an already-claimed boot pane is never retried', async () => {
	const panes = [{ playlist_id: 'all', loading: false, rows: [], selected_ids: [], selected_id: null }];
	let restoreCalls = 0;
	const refresh = makeRefreshLibraryRowsOnce({
		panes,
		source: 'local',
		spotifySelectedId: null,
		getHealth: async () => ({ health: { state_db: { tracks: 5 } } }),
		restoreBootPane: async () => {
			restoreCalls += 1;
		},
		trackCount: 5
	});

	await refresh();

	assert.equal(restoreCalls, 0, 'a pane a user (or a deep link) already claimed must not be re-restored');
});

test('a pending Spotify deep link is never overridden by the retry', async () => {
	const panes = [blankPane()];
	let restoreCalls = 0;
	const refresh = makeRefreshLibraryRowsOnce({
		panes,
		source: 'spotify',
		spotifySelectedId: 'spotify-playlist-1',
		getHealth: async () => ({ health: { state_db: { tracks: 5 } } }),
		restoreBootPane: async () => {
			restoreCalls += 1;
		},
		trackCount: 0
	});

	await refresh();

	assert.equal(
		restoreCalls,
		0,
		'a pending Spotify selection must not be clobbered by an arbitrary local playlist'
	);
});
