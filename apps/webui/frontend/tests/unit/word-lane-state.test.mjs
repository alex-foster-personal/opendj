/**
 * PR-4a word lane state: which lyric overlay a wave row shows, and when the
 * word payload is allowed to cost a fetch.
 *
 * Regression lines:
 * - [if] selectWordLane reports on=true with the lyrics master switch off
 *   [then] the LYR master switch no longer switches anything off - broken
 * - [if] a loaded payload with words does not surface them [then] the word
 *   lane silently degrades to the line lane on tracks that HAVE karaoke
 * - [if] a 404 ('none') entry is not reported unavailable [then] "no data yet"
 *   is indistinguishable from "still loading" and the gutter cannot say so
 * - [if] a loaded payload with ZERO words is not unavailable [then] an empty
 *   lane renders where an honest "nothing aligned" verdict belongs
 * - [if] a fetch error is not surfaced as a string [then] the failure is
 *   swallowed and the row lies about having no lyrics
 * - [if] createWordLaneState loads words while the prefs gate is off [then]
 *   the pref saves nothing and every deck load still pays the request
 * - [if] it loads for a deck with no track [then] null stable ids reach the
 *   cache as fetches for a track that does not exist
 * - [if] LyricLanes stops falling back to main's line lane when the row has no
 *   words [then] PR-4a silently DELETES the lyric overlay decks have today
 * - [if] WordLane paints words before its lane is measured [then] every word
 *   stacks at x=0 on an unlaid-out row - geometry invented from a 0px width
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';
import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/** Real /api/v1/tracks/{id}/lyrics word shapes (KaraokeWordOut). */
const WORDS = [
	{ idx: 0, word: 'hold', start_s: 1.2, end_s: 1.5, line_final: false, score: 0.9, witness: 'agree' },
	{ idx: 1, word: 'on', start_s: 1.6, end_s: 1.9, line_final: true, score: 0.8, witness: 'contradict' }
];
const PREFS_ON = { lyrics_global: true, lyrics_waveform_overlay: true };

const RUNE_ENTRY = [
	"import { createWordLaneState } from '$lib/components/rb/wave/word-lane-state.svelte';",
	'export function mountWordLane(stableId, deps) {',
	'	let lane;',
	'	const stop = $effect.root(() => {',
	'		lane = createWordLaneState(stableId, deps);',
	'	});',
	'	return { lane, stop };',
	'}'
].join('\n');

let selectWordLane;

before(async () => {
	({ selectWordLane } = await loadTypeScriptModule(
		'src/lib/components/rb/wave/word-lane-state.ts'
	));
});

test('the overlay gate is both prefs, independent of what the track has', () => {
	const loaded = { state: 'loaded', track: { words: WORDS }, error: null };
	assert.equal(selectWordLane(loaded, PREFS_ON).on, true);
	assert.equal(
		selectWordLane(loaded, { lyrics_global: false, lyrics_waveform_overlay: true }).on,
		false
	);
	assert.equal(
		selectWordLane(loaded, { lyrics_global: true, lyrics_waveform_overlay: false }).on,
		false
	);
});

test('a loaded payload surfaces its words verbatim', () => {
	const selection = selectWordLane(
		{ state: 'loaded', track: { words: WORDS }, error: null },
		PREFS_ON
	);
	assert.equal(selection.words, WORDS);
	assert.equal(selection.unavailable, false);
	assert.equal(selection.error, null);
});

test('404 and zero-word payloads are the same honest unavailable verdict', () => {
	const none = selectWordLane({ state: 'none', track: null, error: null }, PREFS_ON);
	assert.equal(none.unavailable, true);
	assert.equal(none.words, null);

	const empty = selectWordLane({ state: 'loaded', track: { words: [] }, error: null }, PREFS_ON);
	assert.equal(empty.unavailable, true);
	assert.equal(empty.words, null);
});

test('a fetch failure surfaces as a message and is never an unavailable verdict', () => {
	const selection = selectWordLane(
		{ state: 'error', track: null, error: 'Error: HTTP 500' },
		PREFS_ON
	);
	assert.equal(selection.error, 'Error: HTTP 500');
	assert.equal(selection.unavailable, false);
	assert.equal(selection.words, null);
	assert.throws(
		() => selectWordLane({ state: 'error', track: null, error: null }, PREFS_ON),
		/carries no message/
	);
});

test('an unfetched or trackless row is neither unavailable nor an error', () => {
	for (const entry of [null, { state: 'loading', track: null, error: null }]) {
		const selection = selectWordLane(entry, PREFS_ON);
		assert.equal(selection.words, null);
		assert.equal(selection.unavailable, false);
		assert.equal(selection.error, null);
	}
});

/** Drive the real rune module with an owned cache and prefs. */
async function mount(mountWordLane, { stableId, prefs, entry }) {
	const loaded = [];
	const mounted = mountWordLane(() => stableId, {
		uiPrefs: prefs,
		lyricEntry: () => entry,
		loadLyrics: async (sid) => {
			loaded.push(sid);
		}
	});
	await new Promise((resolve) => setTimeout(resolve, 20));
	return { ...mounted, loaded };
}

test('RUNNING it: the prefs gate decides whether words cost a fetch', async () => {
	const probe = installTimerProbe();
	try {
		const { mountWordLane } = await loadRuneModule(RUNE_ENTRY);

		const off = await mount(mountWordLane, {
			stableId: 'sid-off',
			prefs: { lyrics_global: true, lyrics_waveform_overlay: false },
			entry: null
		});
		assert.equal(off.lane.on, false);
		assert.deepEqual(off.loaded, [], 'overlay off must never fetch word timings');
		off.stop();

		const on = await mount(mountWordLane, {
			stableId: 'sid-on',
			prefs: PREFS_ON,
			entry: { state: 'loaded', track: { words: WORDS }, error: null }
		});
		assert.equal(on.lane.on, true);
		assert.equal(on.lane.words, WORDS);
		assert.deepEqual(on.loaded, ['sid-on']);
		on.stop();

		const empty = await mount(mountWordLane, { stableId: null, prefs: PREFS_ON, entry: null });
		assert.equal(empty.lane.words, null);
		assert.deepEqual(empty.loaded, [], 'a deck with no track must not reach the cache');
		empty.stop();
	} finally {
		probe.restore();
	}
});

const SSR_ENTRY = [
	"export { default as LyricLanes } from '$lib/components/rb/wave/LyricLanes.svelte';",
	"export { default as WordLane } from '$lib/components/rb/wave/WordLane.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

test('RENDERING it: with no words cached, the row keeps the line lane it has today', async () => {
	const ssr = await loadSvelteSsrModule(SSR_ENTRY);
	const body = ssr.render(ssr.LyricLanes, {
		props: {
			stableId: 'sid-ssr',
			lyrics: { lines: [{ start_ms: 1200, text: 'hold on' }] },
			loadError: null,
			positionMs: 1200,
			pitch: 1
		}
	}).body;
	assert.match(body, /lyrics-lane/);
	assert.match(body, /hold on/);
	assert.doesNotMatch(body, /word-lane/, 'the word lane must not pre-empt the line lane');
});

test('RENDERING it: an unmeasured word lane renders its root and zero words', async () => {
	const ssr = await loadSvelteSsrModule(SSR_ENTRY);
	const body = ssr.render(ssr.WordLane, {
		props: { words: WORDS, positionMs: 1200, pitch: 1 }
	}).body;
	assert.match(body, /word-lane/);
	assert.match(body, /aria-label="Karaoke words"/);
	assert.doesNotMatch(body, /hold/, 'a 0px lane has no honest geometry to place a word at');
});
