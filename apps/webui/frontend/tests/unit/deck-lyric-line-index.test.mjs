/**
 * Silver preview, rc f945f7daf4, Tue 6 Oct 2026 09:19:25Z: right after a new
 * track loaded on deck 1, DeckLyricLine threw "Cannot read properties of
 * undefined (reading 'fidelity')". Its guard was `currentLine !== null`, but
 * `track.lines[lineIndex]` with the OLD track's index past the NEW track's
 * lines is undefined, which slips through a null check.
 *
 * Fix: every line selector goes through cursor.ts `lineAtOrNull` (null,
 * never undefined), and the component resets its cursor when `track` changes.
 * This package has no jsdom, so the component half is a structural guard.
 *
 * Single-line acceptance checks:
 * - if a track switch with the old index past the new lines yields undefined -> broken.
 * - if DeckLyricLine indexes track.lines directly again -> broken.
 * - if DeckLyricLine stops resetting its cursor on a track change -> broken.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

function trackWithLines(n) {
	const lines = Array.from({ length: n }, (_, i) => ({ text: `line ${i}`, first_word: i, last_word: i }));
	return { lines, words: lines.map((l, i) => ({ word: l.text, start_s: i, end_s: i + 0.5 })) };
}

test('track switch while the old index exceeds the new lines length: selector is null, not undefined', async () => {
	const { lineAtOrNull } = await loadTypeScriptModule('src/lib/rb/lyrics/cursor.ts');
	const oldTrack = trackWithLines(28);
	const newTrack = trackWithLines(3);
	const staleIndex = 20; // valid on the old track
	assert.equal(lineAtOrNull(oldTrack, staleIndex).text, 'line 20');
	assert.strictEqual(lineAtOrNull(newTrack, staleIndex), null);
	assert.strictEqual(lineAtOrNull(newTrack, staleIndex + 1), null, 'the >> preview row too');
	assert.strictEqual(lineAtOrNull(newTrack, 3), null, 'one past the end');
	assert.strictEqual(lineAtOrNull(newTrack, -1), null);
	assert.strictEqual(lineAtOrNull(newTrack, null), null);
	assert.strictEqual(lineAtOrNull(null, 0), null);
	assert.equal(lineAtOrNull(newTrack, 2).text, 'line 2', 'in range still resolves');
});

test('DeckLyricLine reads every line through lineAtOrNull and resets its cursor on a track change', async () => {
	const src = await readFile('src/lib/components/rb/deck/DeckLyricLine.svelte', 'utf8');
	assert.doesNotMatch(src, /track\.lines\[/, 'no raw track.lines[index] (it can be undefined)');
	for (const name of ['currentLine', 'nextLine', 'thirdLine']) {
		assert.match(src, new RegExp(`const ${name} = \\$derived\\(lineAtOrNull\\(track,`));
	}
	const reset = src.slice(src.indexOf('$effect.pre(() => {'), src.indexOf('const currentLine'));
	assert.match(reset, /void track;/, 'the reset is keyed on the track');
	for (const field of ['lineIndex = null', 'wordIndex = null', 'nextLineIndex = null', 'hint = 0']) {
		assert.ok(reset.includes(field), `track change resets ${field}`);
	}
});
