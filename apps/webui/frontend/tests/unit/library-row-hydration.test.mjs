import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// H20 - library rows arrive fully hydrated; strips never fan out per-row /anlz.
//
//   "IMPORTANT PERFORMANCE CONSTRAINT: the browser table can show thousands of
//    rows. Do NOT eagerly fetch + parse each track's full ANLZ beat-grid on
//    every table render for every row."
//
// This regressed once already ("All Tracks stays on loading... for ages") and
// one ensureAnlz() inside a row render brings it straight back. Backend
// hydration shape is tested; the client's no-fan-out rule was not.
//
// Two halves, because either alone is escapable:
//   BEHAVIOUR - resolveRowVocals answers for N rows out of memory, and a cache
//   miss stays a miss instead of triggering a lookup that could fetch.
//   REACH - the /anlz entry points are confined to the select handler and the
//   deck wave rows, so no per-row component can call them however it is written.
//
// Regression lines:
// - if resolving N rows costs more than 0 fetches then request count scales with
//   visible rows
// - if a cache miss triggers a fetch then a lazy per-row fetch is back on scroll
// - if an unanalyzed deck/cache entry overwrites a hydrated row then a real
//   answer is thrown away for a worse one
// - if a row-rendering component imports ensureAnlz then the fan-out is back
// - if select() stops warming exactly one stable_id then deck load pays the
//   full fetch again

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const BROWSER_PANEL = join(SRC, 'lib/components/rb/BrowserPanel.svelte');
const TRACK_TABLE = join(SRC, 'lib/components/rb/browser/TrackTable.svelte');

/** Every ANLZ fetch entry point. Anything calling these can hit the network. */
// ensureAnlzPrefetch is the shed-gated row-select variant PERFMODE-04 split out
// of ensureAnlz (2f4981be0); it still starts a fetch, so it is an entry point.
// prefetchDeckStripAnlz is the deck strip's untracked wrapper around it (PR
// #4011): listed so its callers stay penned in here, not just the wrapper.
const ANLZ_ENTRY_POINTS = ['ensureAnlz', 'ensureAnlzPrefetch', 'fetchAnlz', 'prefetchDeckStripAnlz'];

/**
 * Files allowed to reach an /anlz fetch, and why. Each is O(1) in the number of
 * library rows: one warm per SELECTED row, one per DECK, and the cache module
 * itself. Adding a per-row component here is the regression.
 */
const ALLOWED_ANLZ_CALLERS = new Map([
	['lib/components/rb/BrowserPanel.svelte', 'one warm fetch for the single selected row'],
	['lib/components/rb/wave/anlz-cache.svelte.ts', 'the cache module itself'],
	['lib/components/rb/wave/WaveRow.svelte', 'one per deck wave row, not per library row'],
	['lib/rb/api-rb.ts', 'the fetch wrapper'],
	['lib/rb/audio-engine.svelte.ts', 'deck load'],
	[
		'lib/player/beatgrid-upgrade.ts',
		'one refetch per deck load, only when a vendor mapping lands mid-flight (PARITY-09)'
	],
	['lib/components/rb/deck/StripWaveform.svelte', 'one per deck strip waveform, not per library row'],
	['routes/mockups/blocks-variance/+page.svelte', 'dev mockup route: one track per page view'],
	['routes/mockups/lyric-rows/+page.svelte', 'dev mockup route: at most two tracks per page view'],
	[
		'lib/components/rb/deck/strip-anlz-prefetch.ts',
		"StripWaveform's untracked warm-up; itself an entry point, so its callers are listed too"
	]
]);

let rowVocals;
let panelSource;

before(async () => {
	rowVocals = await loadTypeScriptModule('src/lib/rb/row-vocals.ts');
	panelSource = readFileSync(BROWSER_PANEL, 'utf8');
});

function sourceFiles(dir) {
	const out = [];
	for (const entry of readdirSync(dir)) {
		const path = join(dir, entry);
		if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
		else if (/\.(svelte|ts)$/.test(entry)) out.push(path);
	}
	return out;
}

/** The full argument text of `marker(...)`, paren-balanced. */
function balancedCall(text, marker) {
	const start = text.indexOf(marker);
	assert.ok(start >= 0, `${marker} not found`);
	let depth = 0;
	for (let i = start + marker.length - 1; i < text.length; i++) {
		if (text[i] === '(') depth++;
		else if (text[i] === ')' && --depth === 0) return text.slice(start, i + 1);
	}
	throw new Error(`unbalanced parens after ${marker}`);
}

const ANALYZED = { status: 'demucs', fps: 10, regions: [{ start_s: 1, end_s: 2, intensity: 0.5 }] };
const NOT_ANALYZED = { status: 'not_analyzed' };
const NO_VOCALS = { status: 'no_vocals', fps: 10, regions: [] };

function rowsOf(count, vocals = NOT_ANALYZED) {
	return Array.from({ length: count }, (_, i) => ({ stable_id: `sid-${i}`, vocals }));
}

// -------------------------------------------------- 0 fetches, whatever N is

test('resolving a whole playlist reads memory only - never a lookup that can fetch', () => {
	// A resolver that could fetch would have to CALL something per row. The only
	// per-row call it makes is the cache read, and this counts every one of them.
	let cacheReads = 0;
	for (const count of [1, 50, 3000]) {
		cacheReads = 0;
		const out = rowVocals.resolveRowVocals({
			rows: rowsOf(count),
			deckVocals: [],
			cachedVocals: (sid) => {
				cacheReads++;
				assert.match(sid, /^sid-\d+$/);
				return undefined; // nothing cached: the common case on a fresh playlist
			}
		});
		assert.equal(Object.keys(out).length, count, `${count} rows did not all resolve`);
		assert.equal(
			cacheReads,
			count,
			'the resolver made more per-row calls than one pure cache read each'
		);
	}
});

test('a row whose vocals were never analyzed renders as such rather than fetching to find out', () => {
	const out = rowVocals.resolveRowVocals({
		rows: [{ stable_id: 'a', vocals: NOT_ANALYZED }],
		deckVocals: [],
		cachedVocals: () => undefined
	});
	assert.deepEqual(
		out.a,
		NOT_ANALYZED,
		'an unanalyzed row must keep saying so - a lazy per-row fetch is the regression'
	);
});

// ------------------------------------------------------------- precedence

test('a loaded deck upgrades its own row and nothing else', () => {
	const out = rowVocals.resolveRowVocals({
		rows: [
			{ stable_id: 'a', vocals: NOT_ANALYZED },
			{ stable_id: 'b', vocals: NOT_ANALYZED }
		],
		deckVocals: [{ stable_id: 'a', vocals: ANALYZED }],
		cachedVocals: () => undefined
	});
	assert.deepEqual(out.a, ANALYZED, 'the loaded deck row did not pick up its real vocals');
	assert.deepEqual(out.b, NOT_ANALYZED, 'an unrelated row was changed by a deck load');
});

test('an already-cached anlz entry upgrades the row it belongs to', () => {
	const out = rowVocals.resolveRowVocals({
		rows: [{ stable_id: 'a', vocals: NOT_ANALYZED }],
		deckVocals: [],
		cachedVocals: (sid) => (sid === 'a' ? ANALYZED : undefined)
	});
	assert.deepEqual(out.a, ANALYZED);
});

test('an unanalyzed deck or cache entry never overwrites a hydrated answer', () => {
	const out = rowVocals.resolveRowVocals({
		rows: [
			{ stable_id: 'a', vocals: NO_VOCALS },
			{ stable_id: 'b', vocals: ANALYZED }
		],
		deckVocals: [{ stable_id: 'a', vocals: NOT_ANALYZED }],
		cachedVocals: (sid) => (sid === 'b' ? NOT_ANALYZED : undefined)
	});
	assert.deepEqual(out.a, NO_VOCALS, 'a real "no vocals" answer was downgraded by a deck load');
	assert.deepEqual(out.b, ANALYZED, 'a real answer was downgraded by an unanalyzed cache entry');
});

test('a row with no listing hydration at all is still answered for', () => {
	const out = rowVocals.resolveRowVocals({ rows: [], deckVocals: [], cachedVocals: () => undefined });
	assert.deepEqual(out, {}, 'an empty pane must resolve to an empty map, not throw');
});

// ------------------------------------------- the fetch entry points are penned in

test('only O(1)-per-view call sites can reach an /anlz fetch', () => {
	const offenders = [];
	for (const path of sourceFiles(SRC)) {
		// The allowlist is keyed by POSIX-shaped paths; join() hands back
		// backslashes on Windows, where every file then read as unlisted.
		const rel = path.slice(SRC.length + 1).replaceAll('\\', '/');
		if (ALLOWED_ANLZ_CALLERS.has(rel)) continue;
		const text = readFileSync(path, 'utf8');
		for (const entry of ANLZ_ENTRY_POINTS) {
			if (new RegExp(`\\b${entry}\\s*\\(`).test(text)) offenders.push(`${rel} calls ${entry}()`);
		}
	}
	assert.deepEqual(
		offenders,
		[],
		'a new file can reach /anlz. If it renders per row, request count now scales with ' +
			'visible rows - the exact "All Tracks stays on loading" regression:\n' +
			offenders.join('\n')
	);
});

test('strip markers resolve from memory: playing deck wins, cache fills, a miss stays absent', () => {
	const asked = [];
	const paused = { cues: [], phrases: [], tag: 'paused' };
	const live = { cues: [], phrases: [], tag: 'playing' };
	const cached = { cues: [], phrases: [], tag: 'cached' };
	const out = rowVocals.resolveRowMarkerAnlz({
		rows: [{ stable_id: 'a' }, { stable_id: 'b' }, { stable_id: 'c' }],
		decks: [
			{ stable_id: 'a', playing: false, anlz: paused },
			{ stable_id: 'a', playing: true, anlz: live },
			{ stable_id: 'a', playing: false, anlz: null }
		],
		cachedAnlz: (sid) => {
			asked.push(sid);
			return sid === 'b' ? cached : undefined;
		}
	});
	assert.equal(out.a, live, 'a playing deck must win over a paused one');
	assert.equal(out.b, cached, 'a ready cache entry answers for a row on no deck');
	assert.equal('c' in out, false, 'a cache miss must stay a miss, not a placeholder');
	assert.deepEqual(asked, ['b', 'c'], 'the cache is consulted once per row with no deck answer');
});

test('preview strips resolve from memory without per-row fetch', () => {
	const hydrated = { cols: 120, bands: new Uint8Array(360), max: 1 };
	const out = rowVocals.resolveRowPreviewStrip({
		rows: [
			{ stable_id: 'a', strip: null },
			{ stable_id: 'b', strip: hydrated },
			{ stable_id: 'c', strip: null }
		],
		cachedAnlzEntry: (sid) => {
			if (sid === 'a') {
				return {
					status: 'ready',
					data: {
						local_waveform: {
							status: 'decoded',
							preview_b64: null,
							preview_max: null
						}
					}
				};
			}
			if (sid === 'c') return { status: 'loading' };
			return undefined;
		}
	});
	assert.equal(out.b, hydrated, 'listing-hydrated strip must win');
	assert.equal(out.a, null, 'decoded-with-null-preview stays absent');
	assert.equal(out.c, null, 'loading cache must not invent strip bytes');
	const loading = rowVocals.resolveRowStripLoading({
		rows: [
			{ stable_id: 'a', strip: null },
			{ stable_id: 'b', strip: hydrated },
			{ stable_id: 'c', strip: null },
			{ stable_id: 'd', strip: null }
		],
		cachedAnlzEntry: (sid) => {
			if (sid === 'c') return { status: 'loading' };
			if (sid === 'd') return undefined;
			return { status: 'ready', data: {} };
		}
	});
	assert.equal(loading.b, false, 'hydrated strip is never loading');
	assert.equal(loading.c, true, 'cache loading shows spinner');
	assert.equal(loading.d, false, 'no cache entry must not spin forever');
});

test('TrackTable draws strip markers from its markerAnlzById prop', () => {
	const table = readFileSync(TRACK_TABLE, 'utf8');
	assert.match(table, /markerAnlz=\{markerAnlzById\[row\.stable_id\] \?\? null\}/);
	assert.match(panelSource, /\{markerAnlzById\}/, 'BrowserPanel no longer passes resolved markers');
	const resolverCall = balancedCall(panelSource, 'resolveRowMarkerAnlz(').replace(/\/\/[^\n]*/g, '');
	assert.ok(resolverCall.includes('getAnlzEntry('), 'the marker resolver no longer reads the cache purely');
	assert.ok(!resolverCall.includes('ensureAnlz'), 'the marker resolver was handed a FETCHING lookup');
});

test('the row-rendering path holds no fetch entry point at all', () => {
	// These render once PER ROW. One ensureAnlz in here is thousands of requests.
	for (const rel of [
		'lib/components/rb/browser/TrackTable.svelte',
		'lib/components/rb/browser/PreviewStrip.svelte'
	]) {
		const text = readFileSync(join(SRC, rel), 'utf8');
		for (const entry of [...ANLZ_ENTRY_POINTS, 'getAnlzEntry']) {
			assert.ok(
				!text.includes(entry),
				`${rel} references ${entry} - the per-row table must render from its props alone`
			);
		}
	}
});

// ------------------------------------------- unmapped rows must hydrate too
//
// #737 made rb-meta.artwork_available a real embedded-tag read for a track
// with no rekordbox mapping (previously pinned to a constant false). The
// lazy hydrator used to skip the round-trip entirely for such rows on the
// (once-true) premise that the payload held nothing new - Codex caught this
// live on PR #773: the skip survived the backend change, so the main
// library browser stayed permanently blind to real embedded artwork.

test('_hydrateRowMeta does not skip the round-trip for unmapped rows', () => {
	const fnStart = panelSource.indexOf('async function _hydrateRowMeta');
	assert.ok(fnStart >= 0, '_hydrateRowMeta not found in BrowserPanel.svelte');
	const fnEnd = panelSource.indexOf('\n\t}', fnStart);
	assert.ok(fnEnd > fnStart, '_hydrateRowMeta body end not found');
	const fnText = panelSource.slice(fnStart, fnEnd);
	assert.ok(
		!/if\s*\(\s*!row\.has_rb_mapping\s*\)\s*return/.test(fnText),
		'_hydrateRowMeta still returns early for !row.has_rb_mapping - unmapped rows never ' +
			'fetch rb-meta, so a real embedded-tag artwork_available is never observed by the ' +
			'main library browser (PR #773 finding)'
	);
});

test('artwork renders from its inline row verdict, not rb-meta hydration', () => {
	const table = readFileSync(TRACK_TABLE, 'utf8');
	const artStart = table.indexOf('class="c-art"');
	const artEnd = table.indexOf('<td class="c-title"', artStart);
	assert.ok(artStart >= 0 && artEnd > artStart, 'artwork cell not found in TrackTable.svelte');
	const artCell = table.slice(artStart, artEnd);
	assert.match(
		artCell,
		/\{#if row\.artwork_available === true\}/,
		'artwork must render from the listing row so disabled rb-meta hydration cannot blank it'
	);
	assert.doesNotMatch(
		artCell,
		/row\.rb_meta/,
		'artwork cell still depends on lazy rb-meta hydration instead of inline artwork facts'
	);
});

test('BrowserPanel warms exactly one anlz, on select, and reads the cache purely elsewhere', () => {
	// PERFMODE-04 (2f4981be0) routes the select warm through ensureAnlzPrefetch,
	// so both the plain and the shed-gated fetch count toward "exactly one".
	const calls = [...panelSource.matchAll(/\bensureAnlz(?:Prefetch)?\s*\(([^)]*)\)/g)].map((m) => m[1].trim());
	assert.deepEqual(
		calls,
		['row.stable_id'],
		'select must warm exactly one stable_id; a batch or a second call site is a fan-out'
	);
	assert.ok(
		panelSource.includes('resolveRowVocals('),
		'BrowserPanel stopped resolving strip vocals through the no-fetch resolver'
	);
	// The strip resolver must be handed the PURE read, never the warming one.
	// Comments are stripped: prose naming ensureAnlz is not a call to it.
	const resolverCall = balancedCall(panelSource, 'resolveRowVocals(').replace(/\/\/[^\n]*/g, '');
	assert.ok(
		resolverCall.includes('getAnlzEntry('),
		'the strip resolver no longer reads the cache purely'
	);
	assert.ok(
		!resolverCall.includes('ensureAnlz'),
		'the strip resolver was handed the FETCHING lookup - every rendered row now fans out'
	);
});
