// perf board row anlz-raw-not-deep-proxy: a deck's ANLZ payload published
// through rawAnlz() is stored RAW inside a deep $state record (no Source
// signal per beat or waveform bin), while replacing st.anlz still re-runs its
// readers. Runs the REAL Svelte runtime via load-rune-module.mjs.
//
// The trade rawAnlz makes: an IN-PLACE edit of the payload no longer notifies
// anyone, so every writer must REPLACE st.anlz. This file pins that from three
// sides:
//   1. the real Svelte runtime: a raw payload is not proxied, a replacement
//      re-runs a reader, and (control) an in-place edit does NOT, while the
//      same in-place edit on a plain payload DOES (the old behavior), so the
//      reactivity tests below can fail;
//   2. one replacement test per production mutation path (hot-cue refresh,
//      authoritative beatgrid adopt, gridless-error publish, beatgrid
//      fallback upgrade, analysis-source refresh, deck swap/load, Rust deck
//      load, eject), driving the real function against captured or served
//      data where it can (upgradeDeckBeatgrid here; refreshAnalysisSourceDecks
//      against the real fixture server in analysis-source-deck-refresh.test.mjs)
//      and the writer's own payload shape where it lives inside the engine class;
//   3. a source scan: every `.anlz = <expr>` writer in src publishes
//      `rawAnlz(...)` or `null`, and nothing edits a deck payload in place.
//
// MUTATION CHECK (measured at this commit, re-measure at your SHA):
//   - rawAnlz() returns its argument without re-parenting      -> tests 2-4, 6-12 fail
//   - beatgrid-upgrade.ts publishes the spread without rawAnlz  -> scan + upgrade test fail
//   - analysis-source-refresh.ts drops rawAnlz                  -> scan + analysis-source-deck-refresh test 1 fail
//   - audio-engine refreshHotCues drops rawAnlz                 -> scan fails
//   - an in-place `st.anlz.cues = x` added to src               -> in-place scan fails
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, test } from 'node:test';

import { loadVerifiedJsonCapture } from './fixtures/verified-capture.mjs';
import { loadRuneModule } from './load-rune-module.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SRC_ROOT = join(FRONTEND_ROOT, 'src');

const ENTRY = [
	"export { rawAnlz, isRawAnlz } from '$lib/rb/anlz-raw';",
	"export { upgradeDeckBeatgrid } from '$lib/player/beatgrid-upgrade';",
	"export { analysisSourceState } from '$lib/rb/analysis-source-state.svelte';",
	"import { resolveDisplayedAnlz } from '$lib/components/rb/wave/anlz-cache.svelte';",
	"import { flushSync } from 'svelte';",
	'type Deck = { stable_id: string | null; anlz: any; anlz_error: string | null; bpm: number | null; key: string | null };',
	"const blank = (): Deck => ({ stable_id: null, anlz: null, anlz_error: null, bpm: null, key: null });",
	'export const decks = $state<Record<1 | 2 | 3 | 4, Deck>>({ 1: blank(), 2: blank(), 3: blank(), 4: blank() });',
	'export function publish(value: unknown, deck: 1 | 2 | 3 | 4 = 1): unknown { decks[deck].anlz = value; return decks[deck].anlz; }',
	// The UI-facing read: WaveRow's own `$derived.by(() => resolveDisplayedAnlz(deck.anlz, deck.stable_id))`.
	'const shown = $derived.by(() => resolveDisplayedAnlz(decks[1].anlz, decks[1].stable_id));',
	'export function watchDeck1(): { seen: unknown[]; stop: () => void } {',
	'	const seen: unknown[] = [];',
	'	const stop = $effect.root(() => {',
	'		$effect(() => {',
	'			const a = shown as any;',
	'			seen.push(a === null ? null : { sid: a.stable_id, beats: a.beatgrid.beats.length, cues: a.cues.length, phrases: a.phrases.length, src: a.beatgrid_source ?? null });',
	'		});',
	'	});',
	'	flushSync();',
	'	return { seen, stop };',
	'}',
	'export { flushSync };'
].join('\n');

const SID = 'a'.repeat(40);
const SID_B = 'b'.repeat(40);

function payload({ sid = SID, beats = 8, cues = 1, phrases = 0, source = 'rekordbox' } = {}) {
	const bands = { length: 0, low: [], mid: [], high: [] };
	return {
		stable_id: sid,
		points: 38400,
		waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
		beatgrid: {
			beat_count: beats,
			beats: Array.from({ length: beats }, (_u, i) => ({ n: (i % 4) + 1, bpm: 128, t: i * 0.46875 }))
		},
		beatgrid_source: source,
		cues: Array.from({ length: cues }, (_u, i) => ({ kind: 'hot_cue', num: i + 1, t: i + 1 })),
		phrases: Array.from({ length: phrases }, (_u, i) => ({ kind: 'up', beat: i * 16 })),
		vocals: { status: 'not_analyzed' }
	};
}

const view = (p) => ({ sid: p.stable_id, beats: p.beatgrid.beats.length, cues: p.cues.length, phrases: p.phrases.length, src: p.beatgrid_source });

const isProxied = (value) => {
	try {
		structuredClone(value);
		return false;
	} catch (error) {
		if (error instanceof DOMException && error.name === 'DataCloneError') return true;
		throw error;
	}
};

/** Loads a fresh live module with deck 1 holding `initial` (published raw). */
async function liveDeck(initial = payload()) {
	const live = await loadRuneModule(ENTRY);
	live.decks[1].stable_id = initial === null ? null : initial.stable_id;
	live.publish(live.rawAnlz(initial));
	const watch = live.watchDeck1();
	return { live, ...watch };
}

const originalFetch = globalThis.fetch;
afterEach(() => {
	globalThis.fetch = originalFetch;
});

// ----- 1. the mechanism, on the real runtime --------------------------------

test('1 control: a plain payload assigned into deep $state IS proxied', async () => {
	const live = await loadRuneModule(ENTRY);
	const plain = payload();
	const stored = live.publish(plain);
	assert.notEqual(stored, plain);
	assert.equal(isProxied(stored), true);
});

test('2 rawAnlz payload is stored raw: same identity, not proxied, nested arrays raw', async () => {
	const live = await loadRuneModule(ENTRY);
	const raw = live.rawAnlz(payload());
	const stored = live.publish(raw);
	assert.equal(stored, raw);
	assert.equal(live.isRawAnlz(stored), true);
	assert.equal(isProxied(stored), false);
	assert.equal(isProxied(stored.beatgrid.beats), false);
	// Data and plain-object semantics survive the re-parenting.
	assert.deepEqual(JSON.parse(JSON.stringify(stored)), payload());
	assert.equal(Object.prototype.hasOwnProperty.call(stored, 'beatgrid'), true);
	assert.equal(stored.hasOwnProperty('cues'), true);
	assert.deepEqual({ ...stored }, payload());
	assert.deepEqual(structuredClone(stored), payload());
});

test('3 rawAnlz passes null through and is idempotent; isRawAnlz agrees across bundles', async () => {
	const live = await loadRuneModule(ENTRY);
	const other = await loadRuneModule(ENTRY);
	assert.equal(live.rawAnlz(null), null);
	assert.equal(live.isRawAnlz(null), false);
	assert.equal(live.isRawAnlz(payload()), false);
	const raw = live.rawAnlz(payload(2));
	assert.equal(live.rawAnlz(raw), raw);
	assert.equal(other.rawAnlz(raw), raw);
	assert.equal(live.isRawAnlz(raw), true);
	assert.equal(other.isRawAnlz(raw), true);
});

test('4 control: an IN-PLACE edit of a raw payload does NOT re-run the UI reader (why every writer must replace)', async () => {
	const { live, seen, stop } = await liveDeck(payload({ cues: 1 }));
	live.decks[1].anlz.cues.push({ kind: 'hot_cue', num: 2, t: 2 });
	live.decks[1].anlz.beatgrid = payload({ beats: 32 }).beatgrid;
	live.flushSync();
	stop();
	assert.deepEqual(seen, [view(payload({ cues: 1 }))], 'a raw payload must not be deep-tracked');
});

test('5 control for the control: the same in-place edit on a PLAIN payload DOES re-run it (old behavior)', async () => {
	const live = await loadRuneModule(ENTRY);
	live.decks[1].stable_id = SID;
	live.publish(payload({ cues: 1 }));
	const { seen, stop } = live.watchDeck1();
	live.decks[1].anlz.cues.push({ kind: 'hot_cue', num: 2, t: 2 });
	live.flushSync();
	stop();
	assert.deepEqual(seen, [view(payload({ cues: 1 })), view(payload({ cues: 2 }))]);
});

// ----- 2. one replacement test per production mutation path -----------------

test('6 hot-cue refresh (audio-engine refreshHotCues: st.anlz = rawAnlz(fresh)) re-renders the cue count', async () => {
	const { live, seen, stop } = await liveDeck(payload({ cues: 1 }));
	live.publish(live.rawAnlz(payload({ cues: 3 })));
	live.flushSync();
	stop();
	assert.deepEqual(seen.at(-1), view(payload({ cues: 3 })));
	assert.equal(live.isRawAnlz(live.decks[1].anlz), true);
});

test('7 authoritative beatgrid adopt (beatgrid guards publishDeckAnlz of a spread merge) re-renders the grid', async () => {
	const { live, seen, stop } = await liveDeck(payload({ beats: 8, cues: 2 }));
	const latest = live.decks[1].anlz;
	// The guards' own merge shape: re-read the live payload, overlay the grid.
	live.publish(live.rawAnlz({ ...latest, beatgrid: payload({ beats: 64 }).beatgrid }));
	live.flushSync();
	stop();
	assert.deepEqual(seen.at(-1), view(payload({ beats: 64, cues: 2 })));
	assert.equal(live.isRawAnlz(live.decks[1].anlz), true, 'a spread copy must be re-wrapped or it is deep-proxied again');
});

test('8 gridless-error publish (beatgrid guards: spread with an emptied grid) re-renders to zero beats', async () => {
	const { live, seen, stop } = await liveDeck(payload({ beats: 16 }));
	const base = live.decks[1].anlz;
	const published = { ...base, beatgrid: { source: base.beatgrid.source, beat_count: 0, beats: [] } };
	live.publish(live.rawAnlz(published));
	live.flushSync();
	stop();
	assert.equal(seen.at(-1).beats, 0);
	assert.equal(live.isRawAnlz(live.decks[1].anlz), true);
});

test('9 deck swap / load() (st.anlz = rawAnlz(publishedAnlz)) re-renders the new track, and eject (null) clears it', async () => {
	const { live, seen, stop } = await liveDeck(payload({ sid: SID, beats: 8 }));
	live.decks[1].stable_id = SID_B;
	live.publish(live.rawAnlz(payload({ sid: SID_B, beats: 12, phrases: 3 })));
	live.flushSync();
	assert.equal(live.isRawAnlz(live.decks[1].anlz), true);
	// _clearLoadedTrackState / Rust eject: st.anlz = null.
	live.decks[1].stable_id = null;
	live.publish(null);
	live.flushSync();
	stop();
	assert.deepEqual(seen, [view(payload({ sid: SID, beats: 8 })), view(payload({ sid: SID_B, beats: 12, phrases: 3 })), null]);
});

test('10 Rust deck load (rust-engine loadRustDeck: st.anlz = rawAnlz(anlz)) from an empty deck renders the payload', async () => {
	const { live, seen, stop } = await liveDeck(null);
	live.decks[1].stable_id = SID;
	live.publish(live.rawAnlz(payload({ beats: 4, phrases: 2 })));
	live.flushSync();
	stop();
	assert.deepEqual(seen, [null, view(payload({ beats: 4, phrases: 2 }))]);
	assert.equal(isProxied(live.decks[1].anlz), false);
});

// 11 analysis-source refresh: covered against the REAL fixture server and the
// production refreshAnalysisSourceDecks wrapper (real cache, parser and /anlz
// route) in analysis-source-deck-refresh.test.mjs, whose first test asserts the
// refreshed payload is published raw. No injected ports or fabricated payloads.

test('12 beatgrid fallback upgrade: the REAL upgradeDeckBeatgrid publishes its merged grid raw and re-renders', async () => {
	const captured = loadVerifiedJsonCapture(
		new URL('./fixtures/beatgrid-fallback-unmapped-captured.json', import.meta.url),
		new URL('./fixtures/beatgrid-fallback-unmapped-captured.manifest.json', import.meta.url)
	);
	const realBeats = captured.beatgrid_fallback_ok.body.beatgrid.beats.length;
	assert.ok(realBeats > 0, 'the captured fallback carries a real grid');
	const live = await loadRuneModule(ENTRY);
	live.analysisSourceState.features.beatgrid = 'own';
	live.decks[1].stable_id = SID;
	live.publish(live.rawAnlz(payload({ beats: 0, cues: 2, source: 'own' })));
	const { seen, stop } = live.watchDeck1();
	globalThis.fetch = async (input) => {
		const path = new URL(typeof input === 'string' ? input : input.url, 'https://x.test').pathname;
		const reply = path.endsWith('/rb-meta')
			? captured.rb_meta_ok
			: path.endsWith('/beatgrid-fallback')
				? { status: captured.beatgrid_fallback_ok.status, body: { ...captured.beatgrid_fallback_ok.body, anlz_available: false } }
				: null;
		if (reply === null) throw new Error(`unexpected request in this test: ${path}`);
		return new Response(JSON.stringify(reply.body), { status: reply.status, headers: { 'content-type': 'application/json' } });
	};
	await live.upgradeDeckBeatgrid(1, SID, live.decks[1], () => false);
	live.flushSync();
	stop();
	assert.equal(seen.at(-1).beats, realBeats, 'the reader saw the landed grid');
	assert.equal(seen.at(-1).cues, 2, 'cues kept from the live payload');
	assert.equal(live.isRawAnlz(live.decks[1].anlz), true);
});

// ----- 3. source scan: every writer replaces, raw ------------------------------

function sourceFiles(dir) {
	const out = [];
	for (const name of readdirSync(dir)) {
		const path = join(dir, name);
		if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
		else if (/\.(ts|svelte)$/.test(name) && !name.endsWith('.d.ts')) out.push(path);
	}
	return out;
}

const stripComments = (text) =>
	text.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' ')).replace(/(^|[^:'"`])\/\/.*$/gm, '$1');

/** `.anlz = <rhs>` writers whose rhs is neither rawAnlz(...) nor null. */
function unwrappedWriters(text) {
	const bad = [];
	for (const m of stripComments(text).matchAll(/\.anlz\s*=(?![=>])\s*([^;\n]*)/g)) {
		const rhs = m[1].trim();
		if (!/^(rawAnlz\(|null\b)/.test(rhs)) bad.push(rhs);
	}
	return bad;
}

/** In-place edits of a deck payload: `.anlz.x = ..`, `.anlz.x.push(..)`, `delete .anlz.x`. */
function inPlaceEdits(text) {
	const code = stripComments(text);
	return [
		...code.matchAll(/\.anlz\??\.[\w.?[\]]+\s*(?:=(?![=>])|\+=|-=)/g),
		...code.matchAll(/\.anlz\??\.[\w.?[\]]+\.(?:push|pop|shift|unshift|splice|sort|reverse|fill|copyWithin)\(/g),
		...code.matchAll(/delete\s+[\w.]*\.anlz\./g),
		...code.matchAll(/Object\.assign\(\s*[\w.[\]]*\.anlz\b/g)
	].map((m) => m[0]);
}

test('13 scanner controls: it flags an unwrapped writer and an in-place edit, and passes a wrapped one', () => {
	assert.deepEqual(unwrappedWriters('st.anlz = { ...latest, beatgrid };'), ['{ ...latest, beatgrid }']);
	assert.deepEqual(unwrappedWriters('decks[deck].anlz = fresh;'), ['fresh']);
	assert.deepEqual(unwrappedWriters('st.anlz = rawAnlz(fresh); st.anlz = null; if (st.anlz === x) {}'), []);
	assert.deepEqual(unwrappedWriters('// st.anlz = fresh\n'), [], 'comments are not writers');
	assert.equal(inPlaceEdits('st.anlz.cues = next;').length, 1);
	assert.equal(inPlaceEdits('deckStates[deck].anlz.beatgrid.beats.push(b);').length, 1);
	assert.equal(inPlaceEdits('delete st.anlz.tempo_changes;').length, 1);
	assert.equal(inPlaceEdits('Object.assign(st.anlz, patch);').length, 1);
	assert.equal(inPlaceEdits('if (st.anlz.cues === x) {} const n = st.anlz?.beatgrid.beats.length;').length, 0);
});

test('14 every `.anlz =` writer in src publishes rawAnlz(...) or null, and nothing edits a payload in place', () => {
	const writers = [];
	const unwrapped = [];
	const inPlace = [];
	for (const file of sourceFiles(SRC_ROOT)) {
		const text = readFileSync(file, 'utf8');
		const rel = relative(FRONTEND_ROOT, file);
		for (const m of stripComments(text).matchAll(/\.anlz\s*=(?![=>])/g)) writers.push(`${rel}@${m.index}`);
		for (const rhs of unwrappedWriters(text)) unwrapped.push(`${rel}: .anlz = ${rhs}`);
		for (const hit of inPlaceEdits(text)) inPlace.push(`${rel}: ${hit}`);
	}
	// Positive control: the scan found the known writers (load, refreshHotCues,
	// guard publish, eject x2, Rust load, source refresh, fallback upgrade), so a
	// green result is not a scan that matched nothing.
	assert.ok(writers.length >= 8, `expected at least 8 .anlz writers, found ${writers.length}: ${writers.join(', ')}`);
	assert.deepEqual(unwrapped, [], 'a writer that skips rawAnlz() deep-proxies that deck payload again');
	assert.deepEqual(inPlace, [], 'an in-place edit of a raw payload never re-renders its readers; replace st.anlz instead');
});
