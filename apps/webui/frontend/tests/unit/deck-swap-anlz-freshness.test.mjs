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
	const usableIndex = body.indexOf('isAnlzEntryUsable(latestAnlzEntry)');
	const publishIndex = body.indexOf('const publishedAnlz =');
	const anlzAssignIndex = body.indexOf('st.anlz = publishedAnlz;');
	const loopAssignIndex = body.indexOf('_displayLoopFrom(publishedAnlz.cues, publishedAnlz.beatgrid.beats)');

	assert.ok(freshnessIndex > 0, 'the swap must re-read the shared anlz cache for this stable_id before publishing');
	assert.ok(publishIndex > freshnessIndex, 'publishedAnlz must be derived from the freshly re-read cache entry');
	assert.ok(usableIndex > publishIndex, 'the re-read entry must be checked for usability, not trusted blindly');
	assert.ok(
		anlzAssignIndex > usableIndex,
		'st.anlz must be assigned from the freshness-checked value, not directly from the cache read'
	);
	assert.ok(
		loopAssignIndex > publishIndex,
		"st.loop must derive from the same freshness-checked value used for st.anlz, or the deck's grid and its " +
			'displayed loop can disagree about which answer is authoritative'
	);
	assert.ok(
		!/st\.anlz = candidateAnlz;/.test(body),
		'st.anlz must not fall back to unconditionally publishing the pre-revalidation candidate'
	);

	// candidateAnlz is captured before load()'s awaits (see the const above rt.processor.dispose()
	// checks upstream); the freshness read must happen no earlier than that capture, otherwise it
	// could race the very fetch that produced candidateAnlz rather than only a later revalidation.
	const captureInLoad = source.lastIndexOf('const candidateAnlz = anlz;', swapStart);
	assert.ok(captureInLoad > loadStart, 'candidateAnlz capture not found before the swap');
});
