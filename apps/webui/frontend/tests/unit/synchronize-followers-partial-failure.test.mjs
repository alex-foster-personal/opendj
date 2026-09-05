/**
 * PR #765 follow-up (P2 "Preserve successes after a partial schedule
 * failure", surfaced alongside a P1 asking the sibling suite's fake to
 * exercise this path for real): _synchronizeFollowers's own catch block must
 * never re-stamp a deck that already scheduled successfully before a LATER
 * stage of the same batch failed. beatgrid-resync.test.mjs's "Preserve
 * successful followers on partial failure" test pins the CONTRACT this
 * function must honor from the caller's side; _synchronizeFollowers itself
 * needs a live Web Audio deck to exercise directly (confirmed: no Web Audio
 * polyfill exists anywhere in this repo's unit tests - see that suite's own
 * docstring), so this is a structural guard on its catch block, not a
 * behavioral one - the genuine, non-faked answer to "test the production
 * path" where the production path cannot run in Node.
 *
 * A second, sharper defect surfaced by re-review (Tue 2 Sep 2026): a deck
 * that succeeds THIS round can still carry a NON-null sync_error left over
 * from an earlier, unrelated failed round - nothing in the partial-failure
 * branch ever clears it for a deck that was not in failedDecks, so
 * beatgrid-resync.ts's _reconcileSyncErrors (which disables purely on
 * `sync_error !== null`) incorrectly disables a deck that just succeeded.
 * The fix reads the per-deck sync_error as authoritative and WRITES it every
 * time (message when failed, null when not), never leaving a stale value.
 *
 * MUTATION CHECK (measured Tue 2 Sep 2026; restored: 3/3 pass):
 *   - `succeededDecks` reverted to catch-invisible (declared `const`, scoped
 *     inside the `if (failedDecks.length > 0)` block only)   -> 1 of 3 fails
 *   - the `succeededDecks.includes(...)` skip removed from the follower loop
 *     and the master line                                     -> 1 of 3 fails
 *   - the partial-failure loop reverted to only ASSIGN on failure (never
 *     clear to null for a non-failed schedule entry)           -> 1 of 3 fails
 * Each isolates to exactly the test written for it.
 *
 *   [if] a batch partially fails (one follower's schedule rejects after
 *     another follower's already resolved) [then] the succeeded follower's
 *     sync_error must not be re-stamped by the outer catch just because it
 *     still reads null ⛔️
 *   [if] the sync master itself scheduled successfully before a follower's
 *     schedule failed [then] the master must not be re-stamped either ⛔️
 *   [if] a deck carries a stale sync_error from an earlier round and
 *     succeeds in THIS round's partial-failure batch [then] the
 *     partial-failure branch must overwrite it with null, not leave it set ⛔️
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';

function synchronizeFollowersBody() {
	const source = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const start = source.indexOf('async function _synchronizeFollowers(');
	assert.ok(start > 0, '_synchronizeFollowers not found - this test is reading the wrong file');
	const end = source.indexOf('\nconst _resyncTracking = createBeatgridResyncTracking', start);
	assert.ok(end > start, '_scopedSync marker not found after _synchronizeFollowers');
	return source.slice(start, end);
}

test('succeededDecks is declared at function scope, visible to the catch block below the try', () => {
	const body = synchronizeFollowersBody();
	const tryStart = body.indexOf('\ttry {');
	assert.ok(tryStart > 0, 'try block not found in _synchronizeFollowers');
	const beforeTry = body.slice(0, tryStart);
	assert.ok(
		beforeTry.includes('let succeededDecks'),
		'succeededDecks must be declared before the try block (function scope), ' +
			'not const-scoped inside the if(failedDecks.length > 0) block - ' +
			'otherwise the catch block below cannot see which decks already ' +
			'succeeded and re-stamps every one of them on any later failure'
	);
});

test('the catch block skips every deck already recorded as succeeded before re-stamping sync_error', () => {
	const body = synchronizeFollowersBody();
	const catchStart = body.lastIndexOf('} catch (error) {');
	assert.ok(catchStart > 0, 'catch block not found in _synchronizeFollowers');
	const catchBody = body.slice(catchStart);
	assert.ok(
		catchBody.includes('succeededDecks.includes(deck)'),
		'the catch block must skip decks already in succeededDecks - otherwise a ' +
			'deck that scheduled successfully before a later batch-mate failed gets ' +
			'its sync_error incorrectly overwritten with the generic batch message, ' +
			'which _reconcileSyncErrors (beatgrid-resync.ts) then reads as a real ' +
			'failure and disables Beat Sync on a deck that never actually failed'
	);
	assert.ok(
		catchBody.includes('!succeededDecks.includes(master)'),
		'the master line must carry the same succeededDecks guard, or a master ' +
			'that scheduled successfully gets incorrectly re-stamped too'
	);
});

test('the partial-failure branch clears sync_error for every non-failed schedule entry, not just skips it', () => {
	const body = synchronizeFollowersBody();
	const branchStart = body.indexOf('if (failedDecks.length > 0) {');
	assert.ok(branchStart > 0, 'partial-failure branch not found in _synchronizeFollowers');
	const branchEnd = body.indexOf('\n\t\t}', branchStart);
	const branch = body.slice(branchStart, branchEnd);
	assert.ok(
		/item\.st\.sync_error\s*=\s*failedDecks\.includes\(item\.deck\)\s*\?\s*message\s*:\s*null/.test(branch),
		'a non-failed schedule entry must have sync_error written to null in the ' +
			'same pass that stamps failed entries with the batch message - otherwise ' +
			'a deck carrying a STALE sync_error from an earlier round still reads as ' +
			"failed to beatgrid-resync.ts's _reconcileSyncErrors even though it just " +
			'succeeded this round, and gets incorrectly disabled'
	);
});

test('pending membership is cleared only once the master beatgrid precondition is confirmed, not before it can still throw', () => {
	// discussion_r3914557002 (P1 BLOCKING): the original ordering cleared
	// clearPendingMembership(master) and every follower's BEFORE
	// _requireBeatGrid(masterState, ...) had a chance to throw. A follower
	// marked pending against a gridless master, retried before that master
	// settles, would lose its pending record on THIS attempt even though the
	// attempt itself fails right here for the exact same reason (master still
	// gridless) - so the master's eventual grid landing never retries it, and
	// every later PLAY on that follower repeats the same throw forever, with
	// beat_sync_enabled never reset by this call site (audio-engine.svelte.ts's
	// `play()`, unlike `setBeatSync`, has no .catch() that flips it back off).
	// Moving both clears to AFTER _requireBeatGrid succeeds means a throw there
	// leaves the pending record intact for the master's own later landing to
	// retry, while a genuine grid confirmation still clears any now-stale
	// pending record exactly as before.
	const body = synchronizeFollowersBody();
	const requireBeatGridCall = body.indexOf("_requireBeatGrid(masterState, 'Beat Sync')");
	assert.ok(requireBeatGridCall > 0, "_requireBeatGrid(masterState, 'Beat Sync') call not found");
	const clearMasterCall = body.indexOf('_resyncTracking.clearPendingMembership(master)');
	assert.ok(clearMasterCall > 0, 'clearPendingMembership(master) call not found');
	assert.ok(
		clearMasterCall > requireBeatGridCall,
		'clearPendingMembership(master) must run AFTER _requireBeatGrid(masterState, ...) succeeds, not before it'
	);
	const clearFollowersLoop = body.indexOf(
		'for (const deck of followers) _resyncTracking.clearPendingMembership(deck)'
	);
	assert.ok(clearFollowersLoop > 0, 'clearPendingMembership follower loop not found');
	assert.ok(
		clearFollowersLoop > requireBeatGridCall,
		'the followers clearPendingMembership loop must also run AFTER _requireBeatGrid(masterState, ...) succeeds'
	);
});
