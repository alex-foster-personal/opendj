// DECKUX-8360: a mirrored deck is a true horizontal mirror (the maintainer, Tue 3 Nov 2026).
// - if a mirrored header still puts the art on the left then the art is not on the outer edge -> broken
// - if a mirrored header keeps BEAT SYNC / MASTER at the far right then sync is not beside the centre -> broken
// - if the mirrored row keeps the jog's Q/SLIP column or loop | jump order then the inner lane is not mirrored -> broken
// - if an unmirrored deck gets any of the reversed rules then decks 1+3 moved -> broken
// -Claude
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(rel) {
	return readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');
}

const deck = source('src/lib/components/rb/Deck.svelte');
const header = source('src/lib/components/rb/deck/DeckHeader.svelte');

test('Deck hands its mirror state to the header and the main row', () => {
	assert.match(deck, /const mirrored: boolean = \$derived\(/);
	assert.match(deck, /<DeckHeader[\s\S]*?\{mirrored\}[\s\S]*?\/>/);
	assert.match(deck, /class:mirror-main-row=\{mirrored\}/);
});

test('mirrored main row also reverses the loop | jump pair and the jog dial | modifier column', () => {
	assert.match(deck, /\.main-row\.mirror-main-row \{\s*flex-direction: row-reverse;/);
	assert.match(
		deck,
		/\.main-row\.mirror-main-row > \.loop-col,\s*\.main-row\.mirror-main-row > \.sel-node > :global\(\.jog\) \{\s*flex-direction: row-reverse;/
	);
});

test('mirrored header flips art, body and sync chrome, and moves the chrome to the inner edge', () => {
	assert.match(header, /<div\s+class="deck-header"\s+class:mirrored\s+data-mirrored=\{mirrored\}/);
	assert.match(
		header,
		/\.deck-header\.mirrored,\s*\.deck-header\.mirrored \.header-body,\s*\.deck-header\.mirrored \.chrome \{\s*flex-direction: row-reverse;/
	);
	assert.match(header, /\.deck-header\.mirrored \.chrome \{\s*margin-left: 0;\s*margin-right: auto;/);
});

test('every mirror rule is scoped to the mirrored class, so unmirrored decks keep the old order', () => {
	const reverses = [...(deck + header).matchAll(/([^{}]+)\{[^{}]*flex-direction: row-reverse;/g)].map((m) => m[1]);
	assert.ok(reverses.length >= 2);
	for (const selector of reverses) assert.match(selector, /mirror/, `unscoped row-reverse: ${selector.trim()}`);
	assert.doesNotMatch(header, /transform:\s*scaleX\(-1\)|direction:\s*rtl/, 'glyphs and text must not flip');
});
