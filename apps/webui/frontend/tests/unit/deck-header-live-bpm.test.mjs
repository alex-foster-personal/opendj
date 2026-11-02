// requirement: DECKGAP-7691d
// Ported with Preview 153230a362 (pin 65b1138bd823): the deck header's large
// BPM is the jog wheel's live tempo (grid interval at the playhead times
// pitch); "(from N bpm)" stays the analyzed original.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const read = (path) => readFileSync(new URL(`../../src/lib/components/rb/deck/${path}`, import.meta.url), 'utf8');
const header = read('DeckHeader.svelte');
const jog = read('JogDial.svelte');

/** The liveBpm derivation, whitespace-normalized, so header and wheel can be compared. */
function liveBpmCall(src) {
	const start = src.indexOf('const liveBpm: number | null = $derived(');
	assert.notEqual(start, -1, 'no liveBpm derivation');
	return src.slice(start, src.indexOf('\n\t);', start)).replace(/\s+/g, ' ');
}

test('the large BPM is derived exactly as the jog wheel derives its BPM', () => {
	// if deck.bpm alone drives it, a pitched deck reads "122 from 122" while the wheel shows grid BPM x pitch -- broken.
	assert.equal(liveBpmCall(header), liveBpmCall(jog));
	assert.match(header, /const bpmText: string = \$derived\(liveBpm === null \? '--\.--' : liveBpm\.toFixed\(2\)\);/);
});

test('the "(from N bpm)" line keeps the analyzed original', () => {
	assert.match(header, /from \{deck\.bpm\?\.toFixed\(0\) \?\? '--'\}bpm/);
	// Control: the large readout must not fall back to the tag BPM text.
	assert.doesNotMatch(header, /const bpmText: string = \$derived\(deck\.bpm === null/);
});

test('the large BPM explains itself on hover', () => {
	assert.match(header, /class="bpm"[\s\S]{0,200}title=\{bpmTitle\}/);
});
