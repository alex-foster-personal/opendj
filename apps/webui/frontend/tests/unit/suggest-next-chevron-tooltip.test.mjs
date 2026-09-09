/**
 * Pin 946e04da2d0d (`/performance`, anchor `.cand-btn`, batch 9b-1).
 *
 * "chevon sometimes hidden under next NEXT tile. button not clearly attached
 * to the tile to the left, probably highlight both (surroudning div) with
 * border and hover on... hover tooltip should be more informative and
 * non-native + 50ms debounce."
 *
 * Companion pin dd4f0f5ae33f (the native-title/custom-popover overlap this
 * pin's new explainer creates) is pinned separately in
 * suggest-next-chevron-title-overlap.test.mjs.
 *
 * Source-level regression, matching the repo idiom for .svelte assertions
 * (see suggest-next-strip.test.mjs's own header): no jsdom/happy-dom mount
 * infra exists here, so these pin the text of the component instead.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const STRIP = fileURLToPath(
	new URL('../../src/lib/components/rb/SuggestNextStrip.svelte', import.meta.url)
);
const stripSource = readFileSync(STRIP, 'utf8');
const stripTemplate = stripSource.slice(stripSource.lastIndexOf('</script>'));
const stripStyle = stripSource.slice(stripSource.lastIndexOf('<style>'));

const EXPLAINER = fileURLToPath(
	new URL('../../src/lib/components/rb/deck/ControlExplainer.svelte', import.meta.url)
);
const explainerSource = readFileSync(EXPLAINER, 'utf8');

// -------------------------------------------------------------------- (1)
// Root cause of the chevron overflowing under the next tile: `.cand-btn` is
// `width: 100%` inside a flex row with no `min-width: 0`, so a flex item's
// default auto min-width (its content's min-content size) lets it grow past
// `.cand`'s 180px cap instead of ellipsizing, spilling paint into the next
// `<li>`, which then paints on top of it (later siblings paint later).

test('the candidate tile has min-width: 0 so it ellipsizes instead of overflowing into the next tile', () => {
	const rule = stripStyle.match(/\.cand-btn\s*\{[^}]*\}/)?.[0] ?? '';
	assert.match(rule, /min-width:\s*0/, 'if .cand-btn has no min-width:0 it can overflow past .cand and get painted over by the next candidate');
});

test('a hovered or focused candidate group is lifted above its neighbors', () => {
	assert.match(stripStyle, /\.cand\s*\{[^}]*position:\s*relative/s, 'the group needs a stacking context to lift above a sibling');
	assert.match(
		stripStyle,
		/\.cand:hover,?\s*\n?\s*\.cand:focus-within\s*\{[^}]*z-index/,
		'if a hovered/focused candidate does not raise z-index it can still be painted under a later sibling'
	);
});

// -------------------------------------------------------------------- (2)
// "button not clearly attached to the tile to the left... highlight both
// (surrounding div) with border and hover" - the border/background moves
// onto the shared `.cand` wrapper so the two buttons read as one grouped
// control, with one hover/focus highlight covering both.

test('the group border lives on the shared .cand wrapper, not split across the two buttons', () => {
	const candRule = stripStyle.match(/\.cand\s*\{[^}]*\}/)?.[0] ?? '';
	assert.match(candRule, /border:/, 'the surrounding div needs the shared border the pin asked for');
	const candBtnRule = stripStyle.match(/\.cand-btn\s*\{[^}]*\}/)?.[0] ?? '';
	const playBtnRule = stripStyle.match(/\.play-btn\s*\{[^}]*\}/)?.[0] ?? '';
	assert.match(candBtnRule, /border:\s*none/, 'a separate border on .cand-btn keeps the two buttons visually detached');
	assert.doesNotMatch(playBtnRule, /border:\s*1px solid var\(--rb-border\)/, 'the chevron should no longer carry its own standalone box border');
});

test('hovering or focusing the group highlights both tile and chevron together', () => {
	assert.match(
		stripStyle,
		/\.cand:hover,?\s*\n?\s*\.cand:focus-within\s*\{[^}]*border-color/,
		'the shared hover/focus state must recolor the shared border so both halves highlight as one control'
	);
});

// -------------------------------------------------------------------- (3)
// Replace the chevron's native title with the repo's existing non-native
// explainer (ControlExplainer), 50ms show debounce, informative copy.

test('SuggestNextStrip imports the existing ControlExplainer instead of inventing a new tooltip', () => {
	assert.match(stripSource, /import ControlExplainer from '\.\/deck\/ControlExplainer\.svelte';/);
});

test('the chevron is wrapped in ControlExplainer with a named 50ms show debounce constant', () => {
	assert.match(stripSource, /const CHEVRON_EXPLAIN_DELAY_MS = 50;/, 'the debounce should be a named, documented constant, matching the *_DEBOUNCE_MS convention used elsewhere in this codebase');
	assert.match(stripTemplate, /<ControlExplainer[^>]*showDelayMs=\{CHEVRON_EXPLAIN_DELAY_MS\}[\s\S]*?<\/ControlExplainer>/);
});

test('the chevron explainer bullets say what pressing the button actually does, not just its destination', () => {
	assert.match(stripSource, /_chevronExplain\(/);
	assert.match(stripSource, /starts? playback immediately/i);
});

// ------------------------------------------------------- ControlExplainer
// The debounce is opt-in per caller ("each has a chosen debounce + reason"),
// so existing ControlExplainer consumers (DeckHeader, JogDial, TransportCluster,
// StemTags) must keep their current instant-show behavior.

test('ControlExplainer exposes an opt-in showDelayMs prop that defaults to 0 (no behavior change for existing callers)', () => {
	assert.match(explainerSource, /showDelayMs\s*=\s*0/);
});

test('ControlExplainer only debounces the show when showDelayMs is greater than 0', () => {
	assert.match(explainerSource, /showDelayMs\s*>\s*0/);
});

test('a pending debounced show is cancelled on close, so a quick hover-out never opens the popover late', () => {
	assert.match(explainerSource, /showTimer/);
	// _close must clear the show timer, not just the hide timer.
	const closeFn = explainerSource.match(/function _close\(\)[^}]*\}/s)?.[0] ?? '';
	assert.match(closeFn, /showTimer/, 'if _close does not clear a pending showTimer, a cancelled hover can still pop the tooltip open late');
});
