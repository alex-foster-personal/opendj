/**
 * Pin 27fe1e3e61b5: partial agent work must still allow follow-up replies on the
 * same marker (not Close-only). Link/selectable halves are in
 * feedback-pin-card-note-links.test.mjs.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const CARD = readFileSync(
	new URL('../../src/lib/components/rb/FeedbackPinCard.svelte', import.meta.url),
	'utf8'
);

test('pin 27fe1e3e61b5 keeps Reply composer outside pinIsDone so partial pins are not Close-only', () => {
	const replyAt = CARD.indexOf('class="fb-reply"');
	assert.notEqual(replyAt, -1, 'textarea follow-up composer must exist');
	const replyBlock = CARD.slice(replyAt, replyAt + 1200);
	assert.match(replyBlock, />Reply</, 'Reply button must be present');
	const doneGateAt = CARD.indexOf('{#if pinIsDone(pin)}');
	assert.notEqual(doneGateAt, -1);
	assert.ok(
		replyAt < doneGateAt,
		'Reply textarea must render before the pinIsDone gate that only adds Archive/Follow-on'
	);
});
