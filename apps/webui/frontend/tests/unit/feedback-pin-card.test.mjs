/** Pin 7f4f3a903343: only reopened cards dismiss on an outside press. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const widget = readFileSync(new URL('../../src/lib/components/rb/FeedbackWidget.svelte', import.meta.url), 'utf8');
const card = readFileSync(new URL('../../src/lib/components/rb/FeedbackPinCard.svelte', import.meta.url), 'utf8');

test('a reopened pin card has a close X and only outside pointer presses dismiss it', () => {
	assert.match(widget, /import FeedbackPinCard from '.\/FeedbackPinCard\.svelte'/);
	assert.match(widget, /<FeedbackPinCard[\s\S]*onclose=\{closePin\}/);
	assert.match(card, /aria-label="Close comment pin"/);
	assert.match(card, /onclick=\{onclose\}>×<\/button/);
	assert.ok(card.includes('<svelte:window onpointerdowncapture={handleOutsidePinPointerDown} />'),
		'outside clicks must close the card before a resize handle stops bubbling');
	const handler = card.slice(card.indexOf('function handleOutsidePinPointerDown'), card.indexOf('<\/script>'));
	assert.match(handler, /pinBodyElement\?\.contains\(target\)/);
	assert.match(handler, /onclose\(\)/);
	const close = widget.slice(widget.indexOf('function closePin'), widget.indexOf('// ----- pin placement'));
	assert.match(close, /openPinId = null/);
	assert.doesNotMatch(close, /pinDraft\s*=\s*null/, 'dismissing a reopened card must retain a new draft');
});
