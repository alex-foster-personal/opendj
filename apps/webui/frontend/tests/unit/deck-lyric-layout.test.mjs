import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('DeckLyricLine renders beside HotCueBank inside cue-flex', async () => {
	const deck = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	const cueBlock = deck.slice(deck.indexOf('class="cue-flex"'), deck.indexOf('class="loop-col"'));
	assert.match(cueBlock, /HotCueBank/);
	assert.match(cueBlock, /DeckLyricLine/);
	const markupPos = deck.indexOf('<DeckLyricLine');
	assert.ok(markupPos >= 0, 'DeckLyricLine must be mounted in markup');
	assert.ok(
		markupPos > deck.indexOf('class="cue-flex"') && markupPos < deck.indexOf('class="loop-col"'),
		'DeckLyricLine must live inside cue-flex beside HotCueBank'
	);
});
