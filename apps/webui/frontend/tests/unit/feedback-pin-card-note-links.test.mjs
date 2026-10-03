/**
 * Pin 27fe1e3e61b5: agent replies weren't selectable text and hyperlinks
 * weren't clickable; follow-up replies on partial/open pins use FB-13 Reply.
 *
 * Root cause of the non-selectable text: .perf-root sets `user-select: none`
 * globally (theme.css), inherited by .fb-note with nothing overriding it.
 *
 * Root cause of the dead links: agent_note was rendered as one plain text
 * node, so a bare http/https URL was never a real anchor.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let fb;
before(async () => {
	fb = await loadTypeScriptModule('src/lib/rb/feedback.ts');
});

const CARD = readFileSync(
	new URL('../../src/lib/components/rb/FeedbackPinCard.svelte', import.meta.url),
	'utf8'
);

test('pin 27fe1e3e61b5 agent reply text is user-selectable, overriding perf-root user-select:none', () => {
	const styles = CARD.slice(CARD.indexOf('<style>'));
	const rule = styles.slice(styles.indexOf('.fb-note'), styles.indexOf('.fb-note') + 400);
	assert.match(rule, /user-select:\s*text/, 'the inherited user-select: none must be overridden for the note');
});

test('the note is linkified through the safe pure helper, never innerHTML of untrusted content', () => {
	assert.match(CARD, /import\s*\{[^}]*linkifyAgentNote[^}]*\}\s*from\s*'\$lib\/rb\/feedback'/s);
	assert.doesNotMatch(CARD, /\{@html/, 'agent notes must never be rendered as raw HTML');
});

test('a linkified URL segment renders as a real anchor: new tab, noopener noreferrer, does not dismiss the card', () => {
	const noteBlockAt = CARD.indexOf('fb-note');
	assert.notEqual(noteBlockAt, -1);
	const block = CARD.slice(noteBlockAt, noteBlockAt + 700);
	assert.match(block, /target="_blank"/);
	assert.match(block, /rel="noreferrer noopener"|rel="noopener noreferrer"/);
	assert.match(block, /onclick=\{[^}]*stopPropagation/, 'a click on the link must not bubble up and close the card');
});

test('linkifyAgentNote itself never drops or mangles the original text (regression, shared with feedback-logic)', () => {
	const original = 'fixed, see https://github.com/x/y/pull/9 - thanks';
	const segments = fb.linkifyAgentNote(original);
	assert.equal(segments.map((s) => s.value).join(''), original);
	assert.ok(segments.some((s) => s.type === 'link' && s.value === 'https://github.com/x/y/pull/9'));
});
