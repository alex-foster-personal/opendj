/**
 * ADVERSARIAL (round 3, intentionally RED until fixed): two places where the
 * Web Audio engine ends a follower's continuous phase lock (NAE-19) and never
 * starts it again, so the follower free-runs and drifts (0.05% grid error is
 * 150 ms after five minutes - the drift the lock exists to stop).
 *
 * The engine's sync and election are module-private and need a live
 * AudioContext (no Web Audio polyfill in node; see
 * synchronize-followers-partial-failure.test.mjs), and the headless e2e that
 * would drive them cannot get a beatgrid in fixture mode on this base (see
 * findings.md, "What could not run"). So these are SOURCE-TEXT guards, the
 * repo's accepted fallback for this function. The Rust engine twin of (1) is
 * reproduced BEHAVIORALLY in beat-sync-adversarial-rust-transport.test.mjs
 * test 2.
 *
 * 1. Automatic master election never re-joins followers.
 *    `_electPlayingMaster` (audio-engine.svelte.ts ~line 1152) only assigns
 *    the role. Its automatic callers - master paused or silenced
 *    (`_handleAudibleTransition`), master faded out with another deck on air
 *    (`_maybeHandoffOnAir`), master played out (natural-end), master unloaded
 *    - change the master while followers play. The phase lock then drops each
 *    follower's lock ('master moved', phase-lock-webaudio.ts `_broken`) and,
 *    by design, sends nothing; only the MANUAL `setDeckMaster` re-joins
 *    (`_synchronizeFollowers(deck, followers, { reanchorDecks })`).
 *
 * 2. A partial schedule failure discards the locks of the followers that DID
 *    lock. `_synchronizeFollowers` clears every follower's lock on entry
 *    (`_phaseLock.clear`), and records them only after the
 *    `failedDecks.length > 0` branch, which throws. A batch where one
 *    follower's schedule rejects therefore leaves its succeeded batch-mates
 *    synced once and never phase-locked again.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';

const ENGINE = 'src/lib/rb/audio-engine.svelte.ts';

function body(source, signature, endMarker) {
	const start = source.indexOf(signature);
	assert.ok(start >= 0, `${signature} not found - the guard is pointed at the wrong file`);
	assert.equal(source.indexOf(signature, start + 1), -1, `${signature} is ambiguous`);
	const end = source.indexOf(endMarker, start + signature.length);
	assert.ok(end > start, `end marker ${JSON.stringify(endMarker)} not found after ${signature}`);
	return source.slice(start, end);
}

test('1. an automatic master election re-joins the playing followers to the new master', () => {
	const source = readFrontendSource(ENGINE);
	const elect = body(source, 'function _electPlayingMaster(', '\n}\n');
	const handoff = body(source, 'function _handleAudibleTransition(', '\n}\n');
	const onAir = body(source, 'function _maybeHandoffOnAir(', '\n}\n');
	const automaticPaths = elect + handoff + onAir;
	// Observed on the unfixed code: none of the three bodies re-plans a
	// follower, so the new master inherits followers with no lock.
	assert.match(
		automaticPaths,
		/_synchronizeFollowers\(|_reanchorFollowers\(|resyncFollowers\(/,
		'no automatic election path re-joins followers; their phase locks end at the handoff'
	);
});

test('control: the MANUAL master switch does re-join (so the guard can say yes)', () => {
	const source = readFrontendSource(ENGINE);
	const manual = body(source, 'async setDeckMaster(deck: DeckId, options?: { lock?: boolean }): Promise<void> {', '\n\t}\n');
	assert.match(manual, /_synchronizeFollowers\(deck, followers, \{ reanchorDecks: new Set\(followers\) \}\)/);
});

test('2. a partial schedule failure still records the phase lock of every follower that locked', () => {
	const source = readFrontendSource(ENGINE);
	const sync = body(source, 'async function _synchronizeFollowers(', '\nconst _resyncTracking = createBeatgridResyncTracking');
	const failureBranch = sync.indexOf('if (failedDecks.length > 0) {');
	const failureThrow = sync.indexOf('throw new Error(message', failureBranch);
	const firstRecord = sync.indexOf('_phaseLock.record(');
	assert.ok(failureBranch > 0 && failureThrow > failureBranch && firstRecord > 0, 'anchors located');
	// Fixed means: a record for succeeded decks happens before (or inside)
	// the branch that throws. Observed: the only record is after it.
	assert.ok(
		firstRecord < failureThrow,
		`_phaseLock.record is only reached after the partial-failure throw (record at +${firstRecord}, ` +
			`throw at +${failureThrow}): succeeded followers lose their lock`
	);
});
