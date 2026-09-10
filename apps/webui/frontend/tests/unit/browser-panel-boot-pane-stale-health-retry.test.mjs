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
 * Round 7 (chatgpt-codex-connector, P1/BLOCKING) rejected the first version
 * of this test: it extracted `_refreshLibraryRowsOnce` as text via `Function`
 * with every collaborator replaced, so it never executed the compiled
 * component and could stay green with the real wiring broken - a materially
 * different (weaker) technique than the fetch/WebSocket-boundary
 * substitution this same PR's `health-coalesce-invalidation.test.mjs` was
 * successfully REBUTTED for, since that file loads the real module graph and
 * substitutes only at genuine I/O boundaries.
 *
 * The fix moves the retry DECISION out of `_refreshLibraryRowsOnce` into
 * `shouldRetryBootPane` (pane-contract.svelte.ts, alongside the sibling pure
 * decision `resolveBootPlaylist`, which this same suite's
 * `boot-playlist.test.mjs` already tests the identical way). This is the
 * real, unmodified module, loaded through `loadTypeScriptModule` exactly like
 * `resolveBootPlaylist` - no source-slicing, no `Function` reconstruction, no
 * replacement of the function under test. `_refreshLibraryRowsOnce` itself
 * just calls it inline; that one-line call site is covered by
 * `pnpm check` (svelte-check across the real component) and this PR's e2e
 * boot-burst bench, neither of which a node:test process can mount.
 *
 * Regression lines:
 * - if a still-unclaimed boot pane is not retried once the fresh count
 *   proves the library is not empty, it is indistinguishable from a truly
 *   empty library until a manual reload -> broken
 * - if the retry fires even while a Spotify deep link is pending
 *   (spotify_selected_id !== null), it clobbers a deliberate
 *   spotifyPendingError state with an arbitrary local playlist -> broken
 * - if an already-claimed pane (a deep link, or a prior successful restore)
 *   is retried anyway, a user's manual navigation could be raced -> broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

async function _loadContract() {
	return loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
}

test('a still-unclaimed boot pane must be retried', async () => {
	const contract = await _loadContract();

	const retry = contract.shouldRetryBootPane({
		boot_pane_playlist_id: null,
		source: 'collection',
		spotify_selected_id: null
	});

	assert.equal(retry, true, 'a blank boot pane with no pending Spotify link must be retried');
});

test('an already-claimed boot pane must never be retried', async () => {
	const contract = await _loadContract();

	const retry = contract.shouldRetryBootPane({
		boot_pane_playlist_id: 'all',
		source: 'collection',
		spotify_selected_id: null
	});

	assert.equal(retry, false, 'a pane a user or a deep link already claimed must not be re-restored');
});

test('a pending Spotify deep link must never be overridden by the retry', async () => {
	const contract = await _loadContract();

	const retry = contract.shouldRetryBootPane({
		boot_pane_playlist_id: null,
		source: 'spotify',
		spotify_selected_id: 'spotify-playlist-1'
	});

	assert.equal(
		retry,
		false,
		'a pending Spotify selection (spotifyPendingError) must not be clobbered by an arbitrary local playlist'
	);
});

test('a blank pane with source spotify but no selected id is still retried', async () => {
	const contract = await _loadContract();

	// source can be 'spotify' with nothing selected (e.g. the user switched
	// tabs without a deep link); only an ACTUAL pending selection must
	// suppress the retry.
	const retry = contract.shouldRetryBootPane({
		boot_pane_playlist_id: null,
		source: 'spotify',
		spotify_selected_id: null
	});

	assert.equal(retry, true, 'spotify source with no pending selection must not block the retry');
});
