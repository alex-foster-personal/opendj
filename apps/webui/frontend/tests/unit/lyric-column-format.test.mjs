import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Library lyrics column (karaoke-lyrics feature). Regression lines:
// - if lyricVerdictGlyph does not map all 4 verdicts to distinct glyphs then broken
// - if lyricSyncQualityPct(null) is not null then a missing pct fabricates a number
// - if lyricSyncQualityPct doesn't round((1-red)*100) then the readout lies
// - if an out-of-band pct_witness_red does not throw then bad wire data renders silently
// - if lyricsSortValue doesn't order verdict-major / quality-minor then the sort is wrong
// - if lyricsSortValue(null) is not null then no-data rows stop sorting last
// - if groupLinesIntoParagraphs drops a trailing paragraph without para_final then broken

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/browser/lyric-column.ts');
});

function _summary(overrides = {}) {
	return {
		effective: 'vocal',
		n_words: 169,
		has_words: true,
		pct_witness_red: 0.15,
		source: 'lrclib',
		language_iso3: 'eng',
		override: null,
		...overrides
	};
}

test('verdict glyphs: all 4 verdicts map to distinct compact glyphs', () => {
	const verdicts = ['vocal', 'sparse', 'no-lyrics', 'unknown'];
	const glyphs = verdicts.map((v) => mod.lyricVerdictGlyph(v));
	for (const g of glyphs) {
		assert.equal(typeof g, 'string');
		assert.ok(g.length >= 1, 'glyph must be non-empty');
	}
	assert.equal(new Set(glyphs).size, 4, 'glyphs must be distinct');
});

test('verdict glyph throws on an unknown verdict (fail-fast, no fabricated cell)', () => {
	assert.throws(() => mod.lyricVerdictGlyph('shouting'), /unknown verdict/);
});

test('verdict title appends the human-override marker only when overridden', () => {
	assert.ok(!mod.lyricVerdictTitle(_summary()).includes('override'));
	assert.ok(mod.lyricVerdictTitle(_summary({ override: 'vocal' })).includes('human override'));
});

test('sync quality pct = round((1 - pct_witness_red) * 100)', () => {
	assert.equal(mod.lyricSyncQualityPct(0), 100);
	assert.equal(mod.lyricSyncQualityPct(1), 0);
	assert.equal(mod.lyricSyncQualityPct(0.15), 85);
	assert.equal(mod.lyricSyncQualityPct(0.374), 63);
});

test('sync quality pct: null in -> null out (never fabricate a number)', () => {
	assert.equal(mod.lyricSyncQualityPct(null), null);
});

test('sync quality pct throws on out-of-band wire values', () => {
	assert.throws(() => mod.lyricSyncQualityPct(-0.01), /outside \[0,1\]/);
	assert.throws(() => mod.lyricSyncQualityPct(1.5), /outside \[0,1\]/);
	assert.throws(() => mod.lyricSyncQualityPct(Number.NaN), /outside \[0,1\]/);
});

test('lyricsSortValue: verdict-major (vocal > sparse > unknown > no-lyrics)', () => {
	const vocal = mod.lyricsSortValue(_summary({ effective: 'vocal', pct_witness_red: 1 }));
	const sparse = mod.lyricsSortValue(_summary({ effective: 'sparse', pct_witness_red: 0 }));
	const unknown = mod.lyricsSortValue(
		_summary({ effective: 'unknown', pct_witness_red: 0 })
	);
	const none = mod.lyricsSortValue(
		_summary({ effective: 'no-lyrics', pct_witness_red: 0 })
	);
	// A 0%-sync vocal still ranks above a 100%-sync sparse: verdict is major.
	assert.ok(vocal > sparse, 'vocal must outrank sparse');
	assert.ok(sparse > unknown, 'sparse must outrank unknown');
	assert.ok(unknown > none, 'unknown must outrank no-lyrics');
});

test('lyricsSortValue: quality-minor within the same verdict, judged 0% > no pct', () => {
	const hi = mod.lyricsSortValue(_summary({ pct_witness_red: 0.1 }));
	const lo = mod.lyricsSortValue(_summary({ pct_witness_red: 0.9 }));
	const judgedZero = mod.lyricsSortValue(_summary({ pct_witness_red: 1 }));
	const unjudged = mod.lyricsSortValue(_summary({ pct_witness_red: null }));
	assert.ok(hi > lo, 'higher sync quality must sort higher within a verdict');
	assert.ok(judgedZero > unjudged, 'a judged 0% must outrank no percent at all');
});

test('lyricsSortValue(null) is null so no-data rows sort last (pane contract)', () => {
	assert.equal(mod.lyricsSortValue(null), null);
});

test('groupLinesIntoParagraphs splits on para_final and keeps the trailing group', () => {
	const line = (first_idx, text, para_final) => ({ first_idx, text, para_final });
	const paras = mod.groupLinesIntoParagraphs([
		line(0, 'a', false),
		line(2, 'b', true),
		line(4, 'c', false),
		line(6, 'd', false)
	]);
	assert.equal(paras.length, 2);
	assert.deepEqual(
		paras.map((p) => p.map((l) => l.text)),
		[['a', 'b'], ['c', 'd']]
	);
});

test('groupLinesIntoParagraphs: empty input -> no paragraphs, no phantom group', () => {
	assert.deepEqual(mod.groupLinesIntoParagraphs([]), []);
});
