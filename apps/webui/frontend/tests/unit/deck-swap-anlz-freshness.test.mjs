import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

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
 * the wiring half of this guard reads the engine as text instead of executing
 * the race. The decision itself (`resolvePublishedAnlz`) is a pure function
 * extracted to beatgrid-resync-guards.ts (quality-ratchet file-size
 * remediation, PR #1587) and is exercised for real below.
 *
 * Fifth round (Codex P2 BLOCKING, PR #1587, discussion on line 3065): the same
 * freshness check landed `st.anlz`/`st.anlz_error` but left `st.bpm` set from
 * the pre-revalidation `candidateTrack.bpm` a few lines above. The deck's
 * separately-tracked public BPM (header, IPC, recommendations, autoplay) then
 * disagreed with the grid Beat Sync had just adopted - an own-sourced
 * revalidation carries its own projected bpm, and an authoritative error must
 * clear the field rather than leave the old source's tempo on display.
 *
 * MUTATION CHECK (re-measure at your SHA, do not trust this comment's count):
 *   - the swap goes back to `st.anlz = candidateAnlz;` unconditionally -> fails
 *   - `st.loop` is derived from `candidateAnlz` instead of the freshness-
 *     checked value                                                    -> fails
 *   - the freshness read is moved before `candidateAnlz` is captured (so it
 *     could race the SAME fetch it is meant to be at least as fresh as) -> fails
 *   - the error branch is dropped, or `st.anlz_error` goes back to an
 *     unconditional `null`                                              -> fails
 *   - `st.bpm` goes back to `candidateTrack.bpm ?? null` unconditionally,
 *     ignoring the freshness-checked grid's own projected bpm             -> fails
 *   - `st.bpm` is not cleared to `null` on the authoritative-error branch -> fails
 */

const source = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');

test('load() publishes resolvePublishedAnlz\'s freshness-checked answer at swap time, not the pre-revalidation candidate', () => {
	const loadStart = source.indexOf('async load(deck: DeckId, stable_id: string, options: DeckLoadOptions = {}): Promise<void> {');
	assert.ok(loadStart > 0, 'load() not found - this test is reading the wrong file');
	const swapStart = source.indexOf('await _withDeckSwap(rt, async () => {', loadStart);
	assert.ok(swapStart > loadStart, 'the deck-swap block was not found inside load()');
	const swapEnd = source.indexOf('\n\t\t});', swapStart);
	assert.ok(swapEnd > swapStart, 'could not bound the deck-swap block');
	const body = source.slice(swapStart, swapEnd);

	const freshnessIndex = body.indexOf('const latestAnlzEntry = getAnlzEntry(stable_id);');
	const usableIndex = body.indexOf('isAnlzEntryUsable(latestAnlzEntry)');
	const errorBranchIndex = body.indexOf("latestAnlzEntry?.status === 'error'");
	const resolveCallIndex = body.indexOf('resolvePublishedAnlz(usableAnlz, latestAnlzError, candidateAnlz, candidateTrack.bpm ?? null)');
	const anlzAssignIndex = body.indexOf('st.anlz = publishedAnlz;');
	const anlzErrorAssignIndex = body.indexOf('st.anlz_error = publishedAnlzError;');
	const loopAssignIndex = body.indexOf('displayLoopFrom(publishedAnlz.cues, publishedAnlz.beatgrid.beats)');
	const bpmAssignIndex = body.indexOf('st.bpm = publishedBpm;');

	assert.ok(freshnessIndex > 0, 'the swap must re-read the shared anlz cache for this stable_id before publishing');
	assert.ok(usableIndex > freshnessIndex, 'the re-read entry must be checked for usability, not trusted blindly');
	assert.ok(
		errorBranchIndex > usableIndex,
		'an authoritative error on the re-read entry must be checked as its own branch, not folded into "not usable yet"'
	);
	assert.ok(
		resolveCallIndex > errorBranchIndex,
		'st.anlz/st.anlz_error/st.bpm must all be derived from ONE resolvePublishedAnlz call, not three independent branches'
	);
	assert.ok(
		anlzAssignIndex > resolveCallIndex,
		'st.anlz must be assigned from the freshness-checked value, not directly from the cache read'
	);
	assert.ok(
		anlzErrorAssignIndex > resolveCallIndex,
		'st.anlz_error must be assigned from the same resolved decision, not hardcoded to null underneath it'
	);
	assert.ok(
		loopAssignIndex > resolveCallIndex,
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

	assert.ok(
		bpmAssignIndex > anlzErrorAssignIndex,
		'st.bpm must be assigned from the same freshness-checked decision as st.anlz/st.anlz_error, not earlier ' +
			'from the raw candidateTrack'
	);
	assert.ok(
		!/st\.bpm = candidateTrack\.bpm \?\? null;/.test(body),
		'st.bpm must not go back to being set unconditionally from the pre-revalidation candidate track'
	);

	// candidateAnlz is captured before load()'s awaits (see the const above rt.processor.dispose()
	// checks upstream); the freshness read must happen no earlier than that capture, otherwise it
	// could race the very fetch that produced candidateAnlz rather than only a later revalidation.
	const captureInLoad = source.lastIndexOf('const candidateAnlz = anlz;', swapStart);
	assert.ok(captureInLoad > loadStart, 'candidateAnlz capture not found before the swap');
});

test("resolvePublishedAnlz picks the freshness-checked answer over the pre-revalidation candidate", async () => {
	const guards = await loadTypeScriptModule('src/lib/player/beatgrid-resync-guards.ts');
	const candidateAnlz = {
		cues: [],
		beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] }
	};

	// Usable branch: a freshly re-read ready entry wins over the candidate,
	// and its own projected bpm wins over the candidate track's.
	const usableAnlz = {
		cues: [],
		beatgrid: { source: 'own', status: 'ok', reason: null, beat_count: 1, bpm: 128, beats: [{ t: 0 }] }
	};
	const usable = guards.resolvePublishedAnlz(usableAnlz, null, candidateAnlz, 120);
	assert.equal(usable.anlz, usableAnlz, 'a usable freshness-checked entry must replace the pre-revalidation candidate');
	assert.equal(usable.anlzError, null);
	assert.equal(usable.bpm, 128, "the usable entry's own projected bpm must win over the candidate track's");

	// Usable branch, grid carries no bpm of its own: falls back to the candidate track.
	const usableNoBpm = { ...usableAnlz, beatgrid: { ...usableAnlz.beatgrid, bpm: undefined } };
	const usableFallback = guards.resolvePublishedAnlz(usableNoBpm, null, candidateAnlz, 120);
	assert.equal(usableFallback.bpm, 120, 'a usable entry with no bpm of its own must fall back to the candidate track bpm');

	// Error branch: an authoritative RbApiError must empty the beatgrid, record
	// the error, and clear bpm rather than leave a stale tempo on display.
	const errored = guards.resolvePublishedAnlz(null, 'source_failed', candidateAnlz, 120);
	assert.deepEqual(errored.anlz.beatgrid, { source: 'rekordbox', beat_count: 0, beats: [] });
	assert.equal(errored.anlzError, 'source_failed');
	assert.equal(errored.bpm, null, 'an authoritative error must clear the public bpm, not leave the old candidate tempo');

	// A candidate carrying own-analyzer-only fields must not strand them on
	// the published error payload: they describe the grid just emptied above
	// (Codex P2 BLOCKING, PR #1587, fifth round).
	const candidateWithOwnFields = {
		...candidateAnlz,
		tempo_changes: [{ at_s: 0, bpm_before: 120, bpm_after: 128, confidence: 0.9 }],
		performance_hints: { dynamic_tempo: true }
	};
	const erroredWithOwnFields = guards.resolvePublishedAnlz(null, 'source_failed', candidateWithOwnFields, 120);
	assert.equal(erroredWithOwnFields.anlz.tempo_changes, undefined, 'tempo_changes must not survive the grid it described being emptied');
	assert.equal(erroredWithOwnFields.anlz.performance_hints, undefined, 'performance_hints must not survive the grid it described being emptied');

	// Neither usable nor errored (still loading, or no entry at all): falls
	// through to the pre-revalidation candidate exactly as an ordinary
	// cache-miss load would.
	const fallthrough = guards.resolvePublishedAnlz(null, null, candidateAnlz, 120);
	assert.equal(fallthrough.anlz, candidateAnlz);
	assert.equal(fallthrough.anlzError, null);
	assert.equal(fallthrough.bpm, 120, "the candidate's own beatgrid.bpm is undefined here, so this must fall back to the candidate track bpm");
});

test('reconcileLoopForAuthoritativeGrid re-derives an idle loop but only re-measures an ENGAGED one, and is wired to the guards', () => {
	const fnStart = source.indexOf('reconcileDeckLoop: (deck, anlz) => reconcileLoopForAuthoritativeGrid(deckStates[deck], anlz),');
	assert.ok(
		fnStart > 0,
		'reconcileLoopForAuthoritativeGrid must be wired to the guards as the reconcileDeckLoop dependency'
	);
});

test('reconcileLoopForAuthoritativeGrid: idle loop re-derives, engaged loop keeps its bounds and only re-measures beat_length', async () => {
	// Codex P2 BLOCKING, PR #1587, fourth round: adoptAuthoritativeGrid/
	// adoptAuthoritativeError can replace an already-loaded deck's beatgrid
	// long after load() published st.loop from the OLD beats. A saved-but-
	// idle loop cue is safe to fully re-derive; a currently ENGAGED live loop
	// must keep its own time bounds (re-deriving from cues could silently
	// move a playing loop's in/out points) and only its beat-count readout
	// may follow the new grid.
	const guards = await loadTypeScriptModule('src/lib/player/beatgrid-resync-guards.ts');
	const newBeats = [{ t: 0 }, { t: 0.5 }, { t: 1 }, { t: 1.5 }];
	const newCues = [{ kind: 'loop', active_loop: true, in_ms: 0, out_ms: 1000 }];
	const newAnlz = { cues: newCues, beatgrid: { source: 'own', status: 'ok', reason: null, beat_count: 4, beats: newBeats } };

	const idle = { loop: { in_ms: 5000, out_ms: 6000, engaged: false, beat_length: null } };
	guards.reconcileLoopForAuthoritativeGrid(idle, newAnlz);
	assert.deepEqual(
		idle.loop,
		{ in_ms: 0, out_ms: 1000, engaged: false, beat_length: 2 },
		'an idle (or absent) loop must be fully re-derived from the new grid, same as a fresh load()'
	);

	const engaged = { loop: { in_ms: 5000, out_ms: 6000, engaged: true, beat_length: null } };
	guards.reconcileLoopForAuthoritativeGrid(engaged, newAnlz);
	assert.equal(engaged.loop.in_ms, 5000, 'an ENGAGED live loop must keep its own time bounds untouched');
	assert.equal(engaged.loop.out_ms, 6000, 'an ENGAGED live loop must keep its own time bounds untouched');
	assert.equal(engaged.loop.beat_length, null, 'the engaged loop bounds do not land on a beat in the new grid, so beat_length is null');
});
