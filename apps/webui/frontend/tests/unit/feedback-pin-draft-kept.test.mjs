// requirement: PGCL-7693c
/**
 * Preview pin e2894de158aa (Preview a6a276d33c): an open comment draft is
 * kept when another pin opens. Each composer is its own entry, and a reopened
 * card with typed reply text is not closed by an outside press.
 *
 * Regression lines:
 *  - if opening a second pin replaces the open list then the first card closes
 *  - if a new composer replaces the draft list then the first draft is lost
 *  - if an outside press closes a card with reply text then the text is lost
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const layer = await readFile('src/lib/components/rb/FeedbackPinLayer.svelte', 'utf8');
const card = await readFile('src/lib/components/rb/FeedbackPinCard.svelte', 'utf8');

test('opening another pin appends to the open list instead of replacing it', () => {
	assert.match(layer, /let openPinIds: string\[\] = \$state\(\[\]\)/);
	assert.match(layer, /if \(openPinIds\.includes\(pin\.id\)\) return;/);
	assert.match(layer, /openPinIds = \[\.\.\.openPinIds, pin\.id\]/);
	assert.doesNotMatch(layer, /let openPinId: string \| null/, 'a single open id drops the first card');
});

test('a new composer is appended, never written over an open one', () => {
	assert.match(layer, /function pushPinDraft\(/);
	assert.match(layer, /pinDrafts = \[\.\.\.pinDrafts,/);
});

test('an outside press keeps a card that holds reply text or is opening another comment', () => {
	const at = card.indexOf('function handleOutsidePinPointerDown');
	assert.notEqual(at, -1);
	const handler = card.slice(at, card.indexOf('\n\t}\n', at));
	assert.match(handler, /replyText\.trim\(\) !== ''\) return;/);
	assert.match(handler, /\.fb-pin, \.fb-pin-body, \.fb-bubble/);
	// Control: an empty card is still closed by an outside press.
	assert.match(handler, /onclose\(\)/);
});
