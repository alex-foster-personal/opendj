/**
 * PARITY-02: refreshHotCues (audio-engine.svelte.ts) must reject a stale
 * /anlz answer the same way every other /anlz publisher does -
 * discussion_r3973991964 P1 BLOCKING.
 *
 * A source switch (setAnalysisSource) bumps the shared fetch generation and
 * wipes the anlz-cache (anlz-fetch-generation.ts); every other /anlz
 * publisher (_fetchAndPublish, fetchAnlzForDeckLoad in anlz-cache.svelte.ts)
 * re-checks the generation before writing so a switch mid-flight cannot
 * repopulate the just-wiped cache and a deck with pre-switch bytes.
 * refreshHotCues was the one publisher that skipped this check entirely,
 * so a SAVE/CLEAR-triggered refresh racing a source switch could silently
 * split the deck's own anlz from analysisSourceState forever.
 *
 * This suite runs under plain `node --test` with no Web Audio polyfill
 * (frontend-performance.md), so no test anywhere in it can drive a deck
 * through a real `engine.load()` decode success: `_requireLoaded`'s
 * `rt.processor !== null` gate is unreachable from outside the module, which
 * has no test-only escape hatch. `_requireLoaded` itself is pinned as
 * source text for the identical reason in hot-cue-mapping-gate.test.mjs -
 * this test follows that same precedent for the generation guard.
 *
 * Regression lines:
 * - if refreshHotCues no longer captures the fetch generation BEFORE its
 *   initial /anlz request, or captures it after, then a switch that lands
 *   in the gap between the capture and the request is invisible to the
 *   retry below, same bug as never checking at all
 * - if refreshHotCues stops re-checking the generation AND the confirmed
 *   source, delegating a mismatch to fetchAnlzUntilSourceConfirmed
 *   (anlz-cache.svelte.ts, the primitive it shares with fetchAnlzForDeckLoad),
 *   then a switch mid-flight repopulates the cache and this deck with
 *   pre-switch bytes while analysisSourceState already recorded the new source
 * - if fetchAnlzUntilCurrentGeneration itself stops comparing against
 *   currentAnlzFetchGeneration() on every pass, or stops returning its own
 *   re-fetched answer, then both callers' retry silently becomes a no-op
 * - if fetchAnlzUntilSourceConfirmed stops re-checking the stamped source
 *   against the confirmed selection on every pass, then a direct-publication
 *   caller can install a payload from a since-reverted source that an
 *   EXTERNAL client toggled - a change the generation counter alone never
 *   sees (discussion_r3978049099 P1 BLOCKING)
 * - if `st.anlz` is set from anything other than refreshHotCues' own
 *   `fresh`, then a resolved retry's answer is discarded and the stale
 *   bytes still land
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

function source(relativePath) {
	const text = readFileSync(`${SRC}/${relativePath}`, 'utf8');
	assert.ok(text.length > 0, `${relativePath} read as empty`);
	return text;
}

const AUDIO_ENGINE = 'lib/rb/audio-engine.svelte.ts';
const ANLZ_CACHE = 'lib/components/rb/wave/anlz-cache.svelte.ts';

function refreshHotCuesBody() {
	const text = source(AUDIO_ENGINE);
	const fnStart = text.indexOf('async refreshHotCues(deck: DeckId): Promise<void> {');
	assert.ok(fnStart >= 0, 'refreshHotCues not found in audio-engine.svelte.ts');
	const fnEnd = text.indexOf('\n\t}', fnStart);
	assert.ok(fnEnd > fnStart, 'refreshHotCues body end not found');
	return text.slice(fnStart, fnEnd);
}

test('refreshHotCues captures the fetch generation before its initial /anlz request', () => {
	const body = refreshHotCuesBody();
	const genIndex = body.search(/generation\s*=\s*currentAnlzFetchGeneration\(\)/);
	const fetchIndex = body.indexOf('fetchAnlzBypassingHttpCache(stableId)');
	assert.ok(genIndex >= 0, 'no currentAnlzFetchGeneration() capture found');
	assert.ok(fetchIndex >= 0, 'no fetchAnlzBypassingHttpCache(stableId) call found');
	assert.ok(
		genIndex < fetchIndex,
		'the generation must be captured BEFORE the initial /anlz request - capturing it after ' +
			'leaves a switch that lands in between invisible to the retry loop, the same bug as no check at all'
	);
});

test('refreshHotCues re-checks the generation and the confirmed source, delegating a mismatch to fetchAnlzUntilSourceConfirmed', () => {
	const body = refreshHotCuesBody();

	const freshMatch = body.match(
		/const fresh =\s*\n\s*generation === currentAnlzFetchGeneration\(\) && anlzMatchesConfirmedSource\(initialFresh\)\s*\n\s*\?\s*initialFresh\s*\n\s*:\s*await fetchAnlzUntilSourceConfirmed\(\(\) => fetchAnlzBypassingHttpCache\(stableId\)\);/
	);
	assert.ok(
		freshMatch,
		'no `generation === currentAnlzFetchGeneration() && anlzMatchesConfirmedSource(initialFresh) ? ' +
			'initialFresh : await fetchAnlzUntilSourceConfirmed(...)` found - a source switch (either this ' +
			"client's own, or an external client's direct toggle that never bumps this client's generation) " +
			'that lands after the first /anlz request has nothing forcing a re-fetch, so the stale ' +
			'pre-switch payload gets adopted and re-published into the shared cache'
	);

	const assignEnd = body.indexOf(freshMatch[0]) + freshMatch[0].length;
	const rest = body.slice(assignEnd);
	assert.match(
		rest,
		/st\.anlz\s*=\s*fresh/,
		'st.anlz must be set from `fresh` AFTER the generation and source checks, so a re-fetched answer actually lands'
	);
	assert.ok(
		rest.indexOf('st.anlz = fresh') < rest.indexOf('st.hot_cues'),
		'st.anlz must be published from the resolved `fresh` before hot cues are derived from it'
	);
});

test('fetchAnlzUntilCurrentGeneration (anlz-cache.svelte.ts) retries until its own re-fetch is current', () => {
	const text = source(ANLZ_CACHE);
	const fnStart = text.indexOf('export async function fetchAnlzUntilCurrentGeneration');
	assert.ok(fnStart >= 0, 'fetchAnlzUntilCurrentGeneration not found in anlz-cache.svelte.ts');
	const fnEnd = text.indexOf('\n}', fnStart);
	assert.ok(fnEnd > fnStart, 'fetchAnlzUntilCurrentGeneration body end not found');
	const body = text.slice(fnStart, fnEnd);

	assert.match(
		body,
		/for\s*\(;;\)\s*\{/,
		'fetchAnlzUntilCurrentGeneration must loop until it settles on a current-generation answer, ' +
			'or both refreshHotCues and fetchAnlzForDeckLoad silently lose their retry'
	);
	assert.match(
		body,
		/const generation = currentAnlzFetchGeneration\(\);/,
		'each pass must re-capture the generation, or a switch during the retry itself is missed'
	);
	assert.match(
		body,
		/if\s*\(generation !== currentAnlzFetchGeneration\(\)\)\s*continue;/,
		'a mismatch after the awaited fetch must retry rather than return the stale answer'
	);
});

test('fetchAnlzUntilSourceConfirmed (anlz-cache.svelte.ts) retries until the stamped source is confirmed', () => {
	const text = source(ANLZ_CACHE);
	const fnStart = text.indexOf('export async function fetchAnlzUntilSourceConfirmed');
	assert.ok(fnStart >= 0, 'fetchAnlzUntilSourceConfirmed not found in anlz-cache.svelte.ts');
	const fnEnd = text.indexOf('\n}', fnStart);
	assert.ok(fnEnd > fnStart, 'fetchAnlzUntilSourceConfirmed body end not found');
	const body = text.slice(fnStart, fnEnd);

	assert.match(
		body,
		/for\s*\(;;\)\s*\{/,
		'fetchAnlzUntilSourceConfirmed must loop until it settles on a source-confirmed answer, or a ' +
			"direct-publication caller can install a payload from a since-reverted source that an EXTERNAL " +
			"client's own toggle never bumped this client's generation counter for (discussion_r3978049099 P1 BLOCKING)"
	);
	assert.match(
		body,
		/await fetchAnlzUntilCurrentGeneration\(fetch\)/,
		'each pass must still go through the generation guard - dropping it reopens the mid-flight-switch bug ' +
			'fetchAnlzUntilCurrentGeneration itself exists to close'
	);
	assert.match(
		body,
		/if\s*\(anlzMatchesConfirmedSource\(data\)\)\s*return data;/,
		'an answer must only be returned once its stamped source agrees with the confirmed selection, or a ' +
			'stale one is returned/installed exactly like never checking at all'
	);
});
