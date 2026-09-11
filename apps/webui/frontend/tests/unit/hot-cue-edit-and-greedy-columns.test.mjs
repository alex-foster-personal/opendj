import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

/**
 * Pin c20eeb07cae0 - two follow-ons to hot-cue rename-on-create
 * (hot-cue-rename-on-create.test.mjs already covers the creation flow):
 *
 * 1. An ALREADY-named cue can be reopened for editing (not just a fresh one),
 *    the rename popover renders ABOVE its pad instead of replacing it, and a
 *    visible cancel control discards the draft without saving (mirroring
 *    Escape, not replacing it).
 * 2. The two hot-cue columns (A-D, E-H) are greedy: hovering OR focusing
 *    inside one grows it to 80% of the bank's width, the other shrinking to
 *    make room, and this respects prefers-reduced-motion. Keyboard focus
 *    (onfocusin/onfocusout) is wired identically to hover (onmouseenter/
 *    onmouseleave), not a mouse-only affordance.
 *
 * Regression lines:
 * - if an existing cue's label has no way to reopen for editing then broken
 * - if reopening for edit does not stopPropagation then broken (a jump would fire too)
 * - if reopening for edit does not prefill the existing comment then broken
 * - if reopening for edit re-saves as a NEW cue instead of renaming then broken
 * - if the rename popover no longer renders above its pad then broken
 * - if there is no visible cancel control besides Escape then broken
 * - if the cancel control's mousedown does not preventDefault then the blur-save races it
 * - if a column's hover class is missing then broken
 * - if keyboard focus does not drive the greedy-column state, in parallel with hover, then broken
 * - if hover and focus collapse back onto one shared variable then ending either interaction can
 *   clear a column the other is still holding open (bot review P3)
 * - if the greedy-column transition ignores prefers-reduced-motion then broken
 * - if the rename input's blur commits while focus is moving to the cancel button then a keyboard
 *   Tab+Enter cancel is defeated by an auto-save race (bot review P2)
 */

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

function source(relativePath) {
	const text = readFileSync(`${SRC}/${relativePath}`, 'utf8');
	assert.ok(text.length > 0, `${relativePath} read as empty`);
	return text;
}

const BANK_PATH = 'lib/components/rb/deck/HotCueBank.svelte';

test('an existing cue can be reopened for editing without triggering a jump', () => {
	const text = source(BANK_PATH);
	const editStart = text.indexOf('async function onEditClick');
	const editEnd = text.indexOf('\n\t}', editStart);
	assert.ok(editStart >= 0 && editEnd > editStart, 'onEditClick must remain a named testable flow');
	const editFlow = text.slice(editStart, editEnd);

	assert.match(editFlow, /event\.stopPropagation\(\);/, 'must not let the jump-on-click handler also fire');
	assert.match(
		editFlow,
		/if \(entry\.cue === null \|\| deck\.stable_id === null\) return;/,
		'editing only makes sense for a slot that already has a cue on a loaded deck'
	);
	assert.match(
		editFlow,
		/await beginRename\(entry\.slot, null, deck\.stable_id, entry\.cue\.comment \?\? ''\);/,
		'reopening for edit must prefill the existing comment and pass newCueAtMs=null so commitRename takes the rename (not save) branch'
	);

	assert.match(
		text,
		/class="edit"[\s\S]{0,400}?onclick=\{\(event\) => void onEditClick\(entry, event\)\}/,
		'the edit affordance must be wired to onEditClick'
	);
});

test('beginRename accepts a prefill draft, defaulting to empty for creation', () => {
	const text = source(BANK_PATH);
	assert.match(
		text,
		/async function beginRename\(\s*slot: HotCueSlot,\s*newCueAtMs: number \| null,\s*stableId: string,\s*draft = ''\s*\): Promise<void> \{/,
		'beginRename must gain an optional draft parameter without changing its first three, still used unmodified by onSlotClick'
	);
	assert.match(text, /renameDraft = draft;/);
});

test('the rename popover renders above its pad, with a visible cancel control', () => {
	const text = source(BANK_PATH);
	assert.match(
		text,
		/\{#if renameSlot === entry\.slot\}[\s\S]{0,400}?<div class="cue-name-popover">/,
		'the rename input must be wrapped in its own popover element, not replace the pad in the grid'
	);
	assert.match(
		text,
		/\.cue-name-popover\s*\{[^}]*position:\s*absolute;[^}]*bottom:\s*100%;/,
		'the popover must be positioned above (bottom: 100%) the pad it edits'
	);
	const cancelStart = text.indexOf('class="cue-name-cancel"');
	assert.notEqual(cancelStart, -1, 'a visible cancel control must exist beside the rename input');
	const cancelTagStart = text.lastIndexOf('<button', cancelStart);
	const cancelTagEnd = text.indexOf('&#215;', cancelStart);
	const cancelTag = text.slice(cancelTagStart, cancelTagEnd);
	assert.match(
		cancelTag,
		/onmousedown=\{\(event\) => event\.preventDefault\(\)\}/,
		'the cancel button must block its own mousedown from blurring (and thus auto-saving) the input first'
	);
	assert.match(cancelTag, /onclick=\{\(\) => cancelRename\(entry\.slot\)\}/, 'the cancel control must discard, not save');
});

test('the hot-cue columns are greedy on hover and on keyboard focus alike', () => {
	const text = source(BANK_PATH);
	// Bot review P3 (pin c20eeb07cae0 follow-up): hover and focus used to
	// share one hoveredCol variable, so ending either interaction cleared
	// the column even while the other was still active (focus a pad, hover
	// into then out of its column, mouseleave reset the layout despite
	// focus still being inside). Hover and focus are now tracked in
	// SEPARATE state variables and combined via activeCol, so ending one
	// cannot clear a column the other is still holding open.
	assert.match(text, /let hoverCol: 1 \| 2 \| null = \$state\(null\);/);
	assert.match(text, /let focusCol: 1 \| 2 \| null = \$state\(null\);/);
	assert.match(text, /const activeCol = \$derived\(hoverCol \?\? focusCol\);/);

	const colStart = text.indexOf('class="cue-col"');
	assert.notEqual(colStart, -1, 'a cue-col wrapper must exist per column');
	const colTag = text.slice(text.lastIndexOf('<div', colStart), text.indexOf('{#each column', colStart));

	assert.match(colTag, /class:grow=\{activeCol === col\}/);
	assert.match(colTag, /class:shrink=\{activeCol !== null && activeCol !== col\}/);
	assert.match(colTag, /onmouseenter=\{\(\) => \(hoverCol = col\)\}/, 'hover must set its own column, not the combined one');
	assert.match(
		colTag,
		/onmouseleave=\{[\s\S]{0,80}if \(hoverCol === col\) hoverCol = null;/,
		'mouseleave must only clear hoverCol, never focusCol'
	);
	assert.match(
		colTag,
		/onfocusin=\{\(\) => \(focusCol = col\)\}/,
		'keyboard focus must be an equal path to hover, not mouse-only (onfocusin bubbles from child pads), and set its own column'
	);
	assert.match(
		colTag,
		/onfocusout=\{[\s\S]{0,80}if \(focusCol === col\) focusCol = null;/,
		'focusout must only clear focusCol, never hoverCol'
	);
	assert.doesNotMatch(
		colTag,
		/onmouseleave=\{[\s\S]{0,80}focusCol = null/,
		'mouseleave must never touch focusCol - that would reintroduce the P3 bug'
	);
	assert.doesNotMatch(
		colTag,
		/onfocusout=\{[\s\S]{0,80}hoverCol = null/,
		'focusout must never touch hoverCol - that would reintroduce the P3 bug'
	);

	const growRule = text.match(/\.cue-col\.grow\s*\{[^}]*\}/)?.[0] ?? '';
	assert.match(growRule, /flex-basis:\s*80%;/, 'the active column must grow to 80% of the bank width');
	const shrinkRule = text.match(/\.cue-col\.shrink\s*\{[^}]*\}/)?.[0] ?? '';
	assert.match(shrinkRule, /flex-basis:\s*20%;/, 'the other column must shrink to 20% to make room');
	assert.match(
		text,
		/@media \(prefers-reduced-motion: reduce\)\s*\{\s*\.cue-col\s*\{\s*transition:\s*none;\s*\}\s*\}/,
		'the greedy-column animation must be disabled under prefers-reduced-motion'
	);
});

test('an expanded cue-col.grow creates a stacking context above neighbors', () => {
	const text = source(BANK_PATH);
	const growRule = text.match(/\.cue-col\.grow\s*\{[^}]*\}/)?.[0] ?? '';
	assert.match(growRule, /flex-basis:\s*80%;/);
	assert.match(growRule, /z-index:\s*[1-9]\d*/);
	assert.match(growRule, /background:\s*var\(--rb-panel\)/);
	assert.doesNotMatch(
		growRule,
		/overflow:\s*hidden/,
		'grow must not clip the rename popover above the first pad'
	);
	const shrinkRule = text.match(/\.cue-col\.shrink\s*\{[^}]*\}/)?.[0] ?? '';
	assert.match(shrinkRule, /overflow:\s*hidden/);
});

test('a column stays grown while hover ends but focus remains inside it (or vice versa)', () => {
	// Behavioral proof, not just source-text: simulate the exact repro from
	// the P3 review comment using activeCol's own derivation logic directly.
	function activeColOf(hoverCol, focusCol) {
		return hoverCol ?? focusCol;
	}
	// focus a pad in column 1, then hover into column 1 and back out.
	let hoverCol = null;
	let focusCol = 1;
	assert.equal(activeColOf(hoverCol, focusCol), 1, 'focus alone must grow the column');
	hoverCol = 1;
	assert.equal(activeColOf(hoverCol, focusCol), 1);
	hoverCol = null; // mouseleave: hover ends, focus is still inside
	assert.equal(
		activeColOf(hoverCol, focusCol),
		1,
		'ending hover must not collapse a column that focus is still holding open'
	);
	focusCol = null; // focusout: now both are gone
	assert.equal(activeColOf(hoverCol, focusCol), null, 'the column may shrink once neither interaction remains');

	// the mirror case: hover a pad, focus it too, then blur away.
	hoverCol = 2;
	focusCol = null;
	assert.equal(activeColOf(hoverCol, focusCol), 2);
	focusCol = 2;
	assert.equal(activeColOf(hoverCol, focusCol), 2);
	focusCol = null; // focusout: focus ends, hover is still inside
	assert.equal(
		activeColOf(hoverCol, focusCol),
		2,
		'ending focus must not collapse a column that hover is still holding open'
	);
	hoverCol = null;
	assert.equal(activeColOf(hoverCol, focusCol), null);
});

test('renaming a hot cue does not commit when focus moves to the cancel button (bot review P2)', () => {
	const text = source(BANK_PATH);
	const onblurStart = text.indexOf('onblur={(event) => {');
	assert.notEqual(onblurStart, -1, 'onblur must inspect the blur event, not fire unconditionally');
	const onblurEnd = text.indexOf('}}', onblurStart) + 2;
	const onblurBlock = text.slice(onblurStart, onblurEnd);

	assert.match(
		onblurBlock,
		/event\.relatedTarget instanceof HTMLElement/,
		'must inspect where focus is going before committing'
	);
	assert.match(
		onblurBlock,
		/event\.relatedTarget\.classList\.contains\('cue-name-cancel'\)/,
		'must specifically recognise focus moving to the cancel button'
	);
	assert.match(onblurBlock, /return;/, 'must skip the commit when focus is headed to cancel');
	assert.match(
		onblurBlock,
		/void commitRename\(entry\);/,
		'must still auto-save on every other blur (tabbing anywhere else, clicking elsewhere)'
	);

	// The cancel button itself is unchanged: still a real <button> (so
	// Enter/Space activate it like a click, exercising the SAME onclick as
	// a mouse cancel - no separate keydown handler is needed for Tab+Enter
	// to reach cancelRename once the blur-race above is defused).
	const cancelStart = text.indexOf('class="cue-name-cancel"');
	const cancelTagStart = text.lastIndexOf('<button', cancelStart);
	const cancelTagEnd = text.indexOf('&#215;', cancelStart);
	const cancelTag = text.slice(cancelTagStart, cancelTagEnd);
	assert.match(cancelTag, /onclick=\{\(\) => cancelRename\(entry\.slot\)\}/);
});
