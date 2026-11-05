/**
 * LYR-13: the library Lyrics column shows found-but-unchecked lyrics as a
 * lyric sheet with a pending dot, never as "?".
 *
 * Input is a CAPTURED real server response (manifest beside it), mapped
 * through the production list-row mapper (browser-row-wire.ts
 * rowFromListWire), then through the cell functions LyricColumn.svelte
 * renders. Track B in the capture has 3 cached lyric lines, no verdict row
 * and no stems: the shape of every openair track the maintainer read as "no lyrics".
 * Variants change ONE wire field of a captured row before it goes through
 * the same mapper, so every value still passes the production parser.
 *
 * Regression lines:
 * - if the captured unchecked row maps to "?" then found lyrics read as doubt (LYR-13 broken)
 * - if its title omits the line count or the reason then the hover lies by omission
 * - if the title claims "no stems" when stems are ready or not yet loaded then it guesses a reason
 * - if unknown with no lyric text stops showing "?" then real doubt is hidden
 * - if a judged verdict's mark or title changes then LYR-13 overshot
 * - if unchecked rows sort below bare unknown / no-lyrics then the sort buries found lyrics
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (p) => readFileSync(fileURLToPath(new URL(`../../${p}`, import.meta.url)), 'utf8');
const FIXTURE = 'tests/unit/fixtures/lyric-column-unchecked-captured.json';
const MANIFEST = 'tests/unit/fixtures/lyric-column-unchecked-captured.manifest.json';

let col;
let wire;
let contract;
let captured;
before(async () => {
	col = await loadTypeScriptModule('src/lib/components/rb/browser/lyric-column.ts');
	wire = await loadTypeScriptModule('src/lib/components/rb/browser/browser-row-wire.ts');
	contract = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	const text = read(FIXTURE);
	const entry = JSON.parse(read(MANIFEST)).files['lyric-column-unchecked-captured.json'];
	assert.equal(createHash('sha256').update(text).digest('hex'), entry.sha256, 'capture was edited');
	captured = JSON.parse(text).items;
});

const clone = (v) => JSON.parse(JSON.stringify(v));
const wireByVerdict = (effective) => {
	const found = captured.filter((w) => w.lyrics?.effective === effective);
	assert.equal(found.length, 1, `capture must hold exactly one ${effective} row`);
	return clone(found[0]);
};
const toRow = (w) => wire.rowFromListWire(w, 1);
const uncheckedWire = () => wireByVerdict('unknown');

test('the capture really is the openair shape: unknown verdict, 3 lines, no words, no stems', () => {
	const w = uncheckedWire();
	assert.equal(w.lyrics.n_lines, 3);
	assert.equal(w.lyrics.has_words, false);
	assert.equal(w.stems.status, 'none');
	assert.equal(wireByVerdict('vocal').lyrics.has_words, true);
});

test('LYR-13: the captured unchecked row is a lyric sheet with a pending dot, not "?"', () => {
	const row = toRow(uncheckedWire());
	assert.equal(col.lyricCellState(row.lyrics), 'unchecked');
	const mark = col.lyricCellMark(row.lyrics);
	assert.equal(mark.kind, 'pending');
	assert.deepEqual(mark, col.LYRIC_UNCHECKED_MARK);
	assert.notDeepEqual(mark, col.lyricVerdictMark('unknown'), 'must differ from the bare "?"');
});

test('LYR-13: the captured unchecked row title states the line count and why', () => {
	const row = toRow(uncheckedWire());
	assert.equal(
		col.lyricCellTitle(row.lyrics, row.stems),
		'lyrics found (3 lines), not yet checked: no stems, so no vocal coverage; no word alignment'
	);
});

test('LYR-13: the stem reason follows the row stem status, never guessed', () => {
	const ready = uncheckedWire();
	ready.stems = {
		status: 'ready',
		model: null,
		preset: null,
		overlap: null,
		shifts: null,
		format: 'flac',
		total_bytes: 1,
		groups: {}
	};
	const readyRow = toRow(ready);
	assert.equal(
		col.lyricCellTitle(readyRow.lyrics, readyRow.stems),
		'lyrics found (3 lines), not yet checked: stems present but vocal coverage not measured yet; no word alignment'
	);
	const invalid = uncheckedWire();
	invalid.stems = { status: 'invalid' };
	const invalidRow = toRow(invalid);
	assert.equal(
		col.lyricCellTitle(invalidRow.lyrics, invalidRow.stems),
		'lyrics found (3 lines), not yet checked: stem bundle invalid, so no vocal coverage; no word alignment'
	);
	// LIBM-172: index rows carry no stems until row assets arrive.
	const indexRow = toRow(uncheckedWire());
	assert.equal(
		col.lyricCellTitle(indexRow.lyrics, null),
		'lyrics found (3 lines), not yet checked: vocal coverage not measured (stem status not loaded for this row); no word alignment'
	);
	assert.throws(
		() => col.lyricCellTitle(indexRow.lyrics, { status: 'weird' }),
		/unknown stem status/
	);
});

test('LYR-13: unknown with no lyric text keeps the bare "?"', () => {
	const w = uncheckedWire();
	w.lyrics.n_lines = 0;
	const row = toRow(w);
	assert.equal(col.lyricCellState(row.lyrics), 'unknown');
	assert.deepEqual(col.lyricCellMark(row.lyrics), { kind: 'text', text: '?' });
	assert.equal(
		col.lyricCellTitle(row.lyrics, row.stems),
		'verdict: unknown - not enough signal to judge'
	);
});

test('LYR-13: the captured vocal row keeps its verdict mark and title', () => {
	const row = toRow(wireByVerdict('vocal'));
	assert.equal(col.lyricCellState(row.lyrics), 'judged');
	assert.deepEqual(col.lyricCellMark(row.lyrics), col.lyricVerdictMark('vocal'));
	assert.equal(col.lyricCellTitle(row.lyrics, row.stems), col.lyricVerdictTitle(row.lyrics));
});

test('LYR-13: Lyrics sort puts unchecked above bare unknown and no-lyrics, below vocal; no data last', () => {
	const sortOf = (w) => contract.sortValue(toRow(w), 'lyrics');
	const unchecked = sortOf(uncheckedWire());
	const bare = uncheckedWire();
	bare.lyrics.n_lines = 0;
	const noLyrics = uncheckedWire();
	noLyrics.lyrics.effective = 'no-lyrics';
	noLyrics.lyrics.verdict = 'no-lyrics';
	const none = uncheckedWire();
	none.lyrics = null;
	assert.ok(unchecked > sortOf(bare), 'unchecked must outrank bare unknown');
	assert.ok(unchecked > sortOf(noLyrics), 'unchecked must outrank no-lyrics');
	assert.ok(sortOf(wireByVerdict('vocal')) > unchecked, 'a judged vocal still outranks unchecked');
	assert.equal(sortOf(none), null, 'no lyric data sorts last');
});
