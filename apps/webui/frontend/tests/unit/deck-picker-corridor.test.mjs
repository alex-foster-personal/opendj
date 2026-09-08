import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

/**
 * Pin 27f889893790 (anchor .preview-hit) - the quick-load deck box should:
 *   (a) take up less horizontal space than an inline row of buttons
 *   (b) only show when hovering the track TITLE or ARTWORK cell specifically,
 *       not anywhere on the row (the .c-preview cell, which hosts the mini
 *       preview strip, must never be a trigger)
 *   (c) give a pointer corridor from title/artwork to the deck buttons, so
 *       travelling the short gap does not dismiss the box before the click
 *       lands (100ms grace)
 *   (d) never sit over the mini preview strip (.preview-hit), so the box's
 *       own hitbox cannot swallow a click/seek meant for the strip
 *
 * Recovered from the orphaned PR #1105 (codex/deck-picker-corridor, squash
 * 9f82479b4554) after the stacked branch it lived on was rebuilt from
 * scratch under it (#1095) and silently dropped the corridor. Reimplemented
 * against main's current .deck-btns box (pin 616aaf77b792: title label,
 * per-deck state classes, spinner reuse, row-selected gate, keyboard access
 * via :focus-within) rather than the old markup, which no longer matches
 * main's TrackTable shape.
 *
 * Regression lines:
 * - if the box is anchored inside .c-preview (or any ancestor shared with
 *   .preview-hit) then broken - it can only sit under .c-title, downstream
 *   of the preview column in the DOM/column order
 * - if the CSS reveal selector triggers on hovering anything other than
 *   .c-art / .c-title (e.g. the bare row) then broken
 * - if there is no hide-delay (corridor grace) distinct from the show
 *   transition then broken - an instant hide defeats the corridor
 * - if .c-preview or .preview-hit appears anywhere in the reveal selector
 *   then broken
 * - if `prefers-reduced-motion: reduce` collapses the corridor grace along
 *   with the opacity animation then broken (Sol review round, PR #1355):
 *   the corridor's pointer-events delay is not decorative motion, it is the
 *   mechanism the pin asks for ("should not be hard to get mouse from
 *   artwork/track -> deck choice"). Only the opacity fade may be disabled
 *   for reduced motion.
 */

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

function source(relativePath) {
	const text = readFileSync(`${SRC}/${relativePath}`, 'utf8');
	assert.ok(text.length > 0, `${relativePath} read as empty`);
	return text;
}

const TABLE_PATH = 'lib/components/rb/browser/TrackTable.svelte';

test('the deck-btns box is a child of c-title, not c-preview', () => {
	const text = source(TABLE_PATH);
	const previewCellStart = text.indexOf('<td class="c-preview">');
	const previewCellEnd = text.indexOf('</td>', previewCellStart);
	const previewCellBody = text.slice(previewCellStart, previewCellEnd);
	assert.doesNotMatch(
		previewCellBody,
		/class="deck-btns"/,
		'the quick-load box must not live inside the preview cell any more - it can block the mini preview hitbox'
	);

	const titleCellStart = text.indexOf('class="c-title"');
	const titleCellEnd = text.indexOf('</td>', titleCellStart);
	const titleCellBody = text.slice(titleCellStart, titleCellEnd);
	assert.match(
		titleCellBody,
		/class="deck-btns"/,
		'the quick-load box must be a child of the c-title cell, downstream of the preview column'
	);
});

test('reveal is scoped to hovering c-art / c-title, never the bare row or the preview cell', () => {
	const text = source(TABLE_PATH);
	assert.doesNotMatch(
		text,
		/tbody tr:hover\.rb-row-selected \.deck-btns/,
		'the old whole-row hover trigger must be gone'
	);
	assert.match(
		text,
		/tr\.rb-row-selected:has\(\.c-art:hover, \.c-title:hover\) \.deck-btns/,
		'reveal must be scoped to hovering .c-art or .c-title specifically'
	);
	assert.doesNotMatch(
		text.match(/tr\.rb-row-selected:has\([^)]*\)/)?.[0] ?? '',
		/c-preview|preview-hit/,
		'the preview cell/strip must never be a reveal trigger'
	);
});

test('a 100ms corridor grace delays the hide, distinct from the (near-instant) show', () => {
	const text = source(TABLE_PATH);
	assert.match(
		text,
		/\.deck-btns button\s*\{[^}]*transition: pointer-events 0s 100ms/s,
		'hiding pointer-events must lag 100ms behind losing hover, so the pointer can travel the gap'
	);
	assert.match(
		text,
		/\.deck-btns:hover button,\s*\n?\s*\.deck-btns:focus-within button\s*\{[^}]*transition-delay: 0s/s,
		'once the pointer is over the box itself (or it is focused) the delay must not apply'
	);
});

/**
 * Sol P1 on PR #1533 (review thread 3961773278). Pin fce26c7493b0 moved the box
 * ABOVE its row, which puts it on top of the PREVIOUS row's title cell - and
 * while revealed the whole painted box was `pointer-events: auto`, so it
 * swallowed that row's hover and double-click exactly the way the
 * on-the-line version swallowed its own row's. Interactivity therefore belongs
 * on the buttons alone; the box's padding, border, background and its
 * non-interactive "load to deck:" label must stay transparent so a pointer
 * travelling upward passes through onto the row above instead of latching onto
 * `.deck-btns:hover`. The rendered proof is
 * `tests/e2e/deck-loader-placement.spec.ts`; this pins the mechanism in source.
 */
test('the box itself never takes the pointer - only its buttons do', () => {
	const text = source(TABLE_PATH);
	assert.match(
		text,
		/\.deck-btns\s*\{[^}]*pointer-events: none;[^}]*\}/s,
		'the box hangs over the row above, so the box itself must never be hittable'
	);
	assert.doesNotMatch(
		text,
		/\.deck-btns:hover,\s*\n?\s*\.deck-btns:focus-within\s*\{[^}]*pointer-events: auto/s,
		'revealing must not make the whole box hittable - that is what blocked the row above'
	);
	assert.match(
		text,
		/\.deck-btns:focus-within button\s*\{[^}]*pointer-events: auto;/s,
		'the buttons alone become hittable when the box is revealed'
	);
});

// Superseded by pin fce26c7493b0 (Tue 8 Sep 2026): the box used to be pinned to
// the row's own line (`top: 0; height: 100%`), which is exactly what the maintainer
// reported as blocking the row's double-click. It now floats ABOVE the row, so
// this test pins the anchor (still .c-title, still clear of .c-preview) and the
// new placement instead of the old one. Escaping upwards is only possible
// because .c-title stops clipping and the title text took the ellipsis with it
// onto .title-text - a `td` is `overflow: hidden`, which would erase the box.
test('the box stays anchored inside c-title, clear of the preview column, and floats above the row', () => {
	const text = source(TABLE_PATH);
	assert.match(text, /\.c-title\s*\{\s*position: relative;/);
	assert.match(
		text,
		/\.deck-btns\s*\{[^}]*position: absolute;[^}]*bottom: 100%;[^}]*right: 0;[^}]*max-width: 100%;/s,
		'the box must be anchored to the TOP edge of its row (bottom: 100%), never over the row own line'
	);
	assert.doesNotMatch(
		text,
		/\.deck-btns\s*\{[^}]*height: 100%;/s,
		'a full-row-height box covers the track line and swallows its double-click (pin fce26c7493b0)'
	);
	assert.match(
		text,
		/\.c-title\s*\{[^}]*overflow: visible;/s,
		'the title cell must stop clipping or the above-the-row box is erased by the td overflow'
	);
	assert.match(
		text,
		/\.c-title \.title-text\s*\{[^}]*overflow: hidden;[^}]*text-overflow: ellipsis;/s,
		'the title text keeps its own truncation once the cell stops clipping'
	);
	assert.match(
		text,
		/<span class="title-text"/,
		'the title text must be wrapped so it, not the cell, owns the ellipsis'
	);
});

test('reduced motion disables the opacity fade only - the 100ms corridor grace must survive it', () => {
	const text = source(TABLE_PATH);
	const reducedMotionMatch = text.match(
		/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\.deck-btns button\s*\{([^}]*)\}/
	);
	assert.ok(reducedMotionMatch, 'expected a prefers-reduced-motion override for .deck-btns button');
	const body = reducedMotionMatch[1];
	assert.doesNotMatch(
		body,
		/transition:\s*none\s*;/,
		'`transition: none` under reduced motion kills the pointer-events delay along with the opacity fade - ' +
			'crossing from artwork/title to a deck button would then dismiss the box instantly, breaking the corridor for reduced-motion users'
	);
	assert.match(
		body,
		/pointer-events 0s 100ms/,
		'the 100ms pointer-events hide delay must be explicitly preserved under reduced motion'
	);
});
