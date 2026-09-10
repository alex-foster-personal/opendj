import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';

/**
 * Codex P1 BLOCKING (PR #1587, discussion on audio-engine.svelte.ts:2854): the
 * fire-and-forget `revalidateAnlz` fired earlier in `load()` can settle WHILE
 * this deck is still fetching/decoding, before `st.stable_id` names the track
 * being loaded. `adoptAuthoritativeGrid` then finds no deck to update and
 * drops the correction. If the deck swap below still publishes the
 * pre-revalidation `candidateAnlz` unconditionally, it re-plants the exact
 * stale grid the revalidation just corrected, and nothing re-checks
 * afterward for the rest of the session.
 *
 * Third round (Codex P1 BLOCKING, PR #1587, discussion_r3974208839): the same
 * race can ALSO settle with an explicit `RbApiError` - the selected source
 * failed to revalidate, not merely "no new grid yet". The swap used to read
 * that as just another non-usable entry and fall through to publishing
 * `candidateAnlz` unconditionally, clearing `st.anlz_error` in the process, so
 * the deck completed with a beatgrid known to be untrustworthy and quantize/
 * Beat Sync kept running against it.
 *
 * A live `load()` needs Web Audio (same reasoning as
 * deck-beatgrid-fallback-upgrade.test.mjs and deck-lazy-stems.test.mjs), so
 * this guard reads the engine as text instead of executing the race.
 *
 * MUTATION CHECK (re-measure at your SHA, do not trust this comment's count):
 *   - the swap goes back to `st.anlz = candidateAnlz;` unconditionally -> fails
 *   - `st.loop` is derived from `candidateAnlz` instead of the freshness-
 *     checked value                                                    -> fails
 *   - the freshness read is moved before `candidateAnlz` is captured (so it
 *     could race the SAME fetch it is meant to be at least as fresh as) -> fails
 *   - the error branch is dropped, or `st.anlz_error` goes back to an
 *     unconditional `null`                                              -> fails
 */

const source = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');

test('load() publishes the freshest usable anlz cache entry at swap time, not the pre-revalidation candidate', () => {
	const loadStart = source.indexOf('async load(deck: DeckId, stable_id: string): Promise<void> {');
	assert.ok(loadStart > 0, 'load() not found - this test is reading the wrong file');
	const swapStart = source.indexOf('await _withDeckSwap(rt, async () => {', loadStart);
	assert.ok(swapStart > loadStart, 'the deck-swap block was not found inside load()');
	const swapEnd = source.indexOf('\n\t\t});', swapStart);
	assert.ok(swapEnd > swapStart, 'could not bound the deck-swap block');
	const body = source.slice(swapStart, swapEnd);

	const freshnessIndex = body.indexOf('const latestAnlzEntry = getAnlzEntry(stable_id);');
	const declareIndex = body.indexOf('let publishedAnlz: AnlzData;');
	const usableIndex = body.indexOf('isAnlzEntryUsable(latestAnlzEntry)');
	const errorBranchIndex = body.indexOf("latestAnlzEntry?.status === 'error'");
	const anlzAssignIndex = body.indexOf('st.anlz = publishedAnlz;');
	const anlzErrorAssignIndex = body.indexOf('st.anlz_error = publishedAnlzError;');
	const loopAssignIndex = body.indexOf('_displayLoopFrom(publishedAnlz.cues, publishedAnlz.beatgrid.beats)');

	assert.ok(freshnessIndex > 0, 'the swap must re-read the shared anlz cache for this stable_id before publishing');
	assert.ok(declareIndex > freshnessIndex, 'publishedAnlz must be derived from the freshly re-read cache entry');
	assert.ok(usableIndex > declareIndex, 'the re-read entry must be checked for usability, not trusted blindly');
	assert.ok(
		errorBranchIndex > usableIndex,
		'an authoritative error on the re-read entry must be checked as its own branch, not folded into "not usable yet"'
	);
	assert.ok(
		anlzAssignIndex > errorBranchIndex,
		'st.anlz must be assigned from the freshness-checked value, not directly from the cache read'
	);
	assert.ok(
		anlzErrorAssignIndex > errorBranchIndex,
		'st.anlz_error must be assigned from the same branch decision, not hardcoded to null underneath it'
	);
	assert.ok(
		loopAssignIndex > declareIndex,
		"st.loop must derive from the same freshness-checked value used for st.anlz, or the deck's grid and its " +
			'displayed loop can disagree about which answer is authoritative'
	);
	assert.ok(
		!/st\.anlz = candidateAnlz;/.test(body),
		'st.anlz must not fall back to unconditionally publishing the pre-revalidation candidate'
	);
	assert.ok(
		!/st\.anlz_error = null;\n(\s*st\.processor_error)/.test(body),
		'st.anlz_error must not be unconditionally cleared right after the swap - an authoritative revalidation ' +
			'failure caught in the same race must survive onto the published deck'
	);

	// candidateAnlz is captured before load()'s awaits (see the const above rt.processor.dispose()
	// checks upstream); the freshness read must happen no earlier than that capture, otherwise it
	// could race the very fetch that produced candidateAnlz rather than only a later revalidation.
	const captureInLoad = source.lastIndexOf('const candidateAnlz = anlz;', swapStart);
	assert.ok(captureInLoad > loadStart, 'candidateAnlz capture not found before the swap');
});

test('_reconcileLoopForAuthoritativeGrid re-derives an idle loop but only re-measures an ENGAGED one, and is wired to the guards', () => {
	// Codex P2 BLOCKING, PR #1587, fourth round: adoptAuthoritativeGrid/
	// adoptAuthoritativeError can replace an already-loaded deck's beatgrid
	// long after load() published st.loop from the OLD beats. A saved-but-
	// idle loop cue is safe to fully re-derive; a currently ENGAGED live loop
	// must keep its own time bounds (re-deriving from cues could silently
	// move a playing loop's in/out points) and only its beat-count readout
	// may follow the new grid.
	const fnStart = source.indexOf(
		'function _reconcileLoopForAuthoritativeGrid(st: DeckState, anlz: AnlzData): void {'
	);
	assert.ok(fnStart > 0, '_reconcileLoopForAuthoritativeGrid not found');
	const bodyEnd = source.indexOf('\n}', fnStart);
	assert.ok(bodyEnd > fnStart, 'could not bound the function body');
	const body = source.slice(fnStart, bodyEnd);

	const engagedCheckIndex = body.indexOf('!st.loop.engaged');
	const rederiveIndex = body.indexOf('st.loop = _displayLoopFrom(anlz.cues, anlz.beatgrid.beats);');
	const returnIndex = body.indexOf('return;');
	const beatLengthIndex = body.indexOf(
		'st.loop.beat_length = pqtzLoopBeatCount(anlz.beatgrid.beats, st.loop.in_ms, st.loop.out_ms);'
	);

	assert.ok(engagedCheckIndex > 0, 'must branch on whether the current loop is engaged, not treat every loop alike');
	assert.ok(
		rederiveIndex > engagedCheckIndex,
		'an idle (or absent) loop must be fully re-derived from the new grid, same as a fresh load()'
	);
	assert.ok(returnIndex > rederiveIndex, 'the idle branch must not also fall through into the engaged branch');
	assert.ok(
		beatLengthIndex > returnIndex,
		'an engaged loop must recompute beat_length in place rather than being silently skipped entirely'
	);

	assert.ok(
		source.includes('reconcileDeckLoop: (deck, anlz) => _reconcileLoopForAuthoritativeGrid(deckStates[deck], anlz),'),
		'_reconcileLoopForAuthoritativeGrid must be wired to the guards as the reconcileDeckLoop dependency'
	);
});
