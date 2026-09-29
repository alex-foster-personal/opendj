import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Library lyrics column (karaoke-lyrics feature). Regression lines:
// - if lyricVerdictMark does not map all 4 verdicts to distinct marks then broken
// - if the vocal verdict is a text character rather than an SVG icon path then CHROME-01 is broken
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

test('verdict marks: all 4 verdicts map to distinct compact marks', () => {
	const verdicts = ['vocal', 'sparse', 'no-lyrics', 'unknown'];
	const marks = verdicts.map((v) => mod.lyricVerdictMark(v));
	for (const m of marks) {
		const value = m.kind === 'icon' ? m.path : m.text;
		assert.ok(m.kind === 'icon' || m.kind === 'text', `unexpected mark kind ${m.kind}`);
		assert.equal(typeof value, 'string');
		assert.ok(value.length >= 1, 'mark must be non-empty');
	}
	assert.equal(new Set(marks.map((m) => `${m.kind}:${m.kind === 'icon' ? m.path : m.text}`)).size, 4, 'marks must be distinct');
});

test('verdict mark: the vocal verdict is an SVG icon path, not a note character (CHROME-01)', () => {
	const vocal = mod.lyricVerdictMark('vocal');
	assert.equal(vocal.kind, 'icon');
	assert.match(vocal.path, /^M/);
});

test('verdict mark throws on an unknown verdict (fail-fast, no fabricated cell)', () => {
	assert.throws(() => mod.lyricVerdictMark('shouting'), /unknown verdict/);
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
