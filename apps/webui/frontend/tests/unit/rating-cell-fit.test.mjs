/**
 * The rating cell must fit five stars, never ellipsise them.
 *
 * SOURCE-SHAPE ON PURPOSE (see capability-gating-markup.test.mjs): no
 * component mount infra here, and the fact under test is a CSS contract.
 *
 * The rating cell must reduce star gaps, then glyph size, before truncating.
 *
 * Five ★ at 11px with 1px gaps come to
 * about 67px of advance width inside 68px of usable cell, so the row sits a
 * hair over the edge - the stars all paint, and the generic `td` rule
 * (white-space: nowrap; overflow: hidden; text-overflow: ellipsis) then adds
 * an ellipsis for the sliver that did not fit. Truncation was never the right
 * answer for a fixed five-glyph control: it has two ways to give ground
 * first: reduce gaps, then glyph size.
 *
 * Regression lines:
 * - if .c-rating inherits text-overflow: ellipsis again then five visible
 *   stars still render a trailing '..'
 * - if the cell stops publishing its own width then neither the gap stage nor
 *   the size stage has anything to measure against and both silently no-op
 * - if the star glyph goes back to a fixed font-size then stage two is gone
 *   and a narrowed rating column truncates again
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

const table = read('src/lib/components/rb/browser/TrackTable.svelte');
const theme = read('src/lib/rb/theme.css');
const stars = read('src/lib/components/rb/browser/RatingStars.svelte');

/** The `.c-rating { ... }` block of TrackTable's stylesheet. */
function ratingRule() {
	const start = table.indexOf('\t.c-rating {');
	assert.notEqual(start, -1, 'no .c-rating rule in TrackTable');
	return table.slice(start, table.indexOf('\n\t}', start));
}

test('the rating cell clips rather than ellipsises', () => {
	assert.match(ratingRule(), /text-overflow:\s*clip/);
});

test('the cell publishes its own width so the two stages can measure', () => {
	// The width is already state (colWidths.rating); the cell has to hand it
	// to CSS or neither stage has a budget to fit into.
	assert.match(table, /--rating-w:\$\{colWidths\.rating\}px/);
	assert.match(ratingRule(), /--rating-avail:/);
});

test('stage one closes the gaps before stage two shrinks the glyphs', () => {
	const rule = ratingRule();
	assert.match(rule, /--rb-star-gap:\s*clamp\(/, 'no gap stage');
	assert.match(rule, /--rb-star-size:\s*min\(/, 'no size stage');
	// The gap stage must be allowed to reach zero, or it is not a stage.
	assert.match(rule, /--rb-star-gap:\s*clamp\(\s*0px/);
	// And the size stage must never exceed the normal browser text size.
	assert.match(rule, /--rb-star-size:\s*min\(\s*var\(--rb-fs-browser\)/);
});

test('unrated stars reserve all four tripled gaps within the cell budget', () => {
	assert.match(stars, /gap:\s*calc\(3 \* var\(--rb-star-gap,/);
	assert.match(ratingRule(), /calc\(\(var\(--rating-avail\) - 5 \* 1\.2 \* var\(--rb-fs-browser\)\) \/ 12\)/);
});

test('the star glyph reads both variables, with safe standalone fallbacks', () => {
	assert.match(theme, /\.perf-root \.rb-star \{[^}]*font-size:\s*var\(--rb-star-size,\s*var\(--rb-fs-browser\)\)/s);
	assert.match(stars, /gap:\s*var\(--rb-star-gap,/);
});

test('the star SVG is sized in em so it follows the shrunken star size', () => {
	assert.match(stars, /<svg viewBox="0 0 24 24" width="1em" height="1em"/);
	assert.doesNotMatch(stars, /<svg[^>]*width="12"/);
});
