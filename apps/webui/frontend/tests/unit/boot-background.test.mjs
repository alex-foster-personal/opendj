// requirement: PERF-UI-08, LIBUX-30
// [if] the document has been parsed but no stylesheet has arrived [then] it
// already paints the app background, and [if] technically-working mode hides
// a region [then] none of that region's separator rules stay painted
//
// Regression lines:
// - if app.html carries no background of its own then every load shows the
//   browser's default canvas until the app stylesheet lands
// - if the boot background and app.css's --bg drift apart then the page
//   changes color the moment the stylesheet lands
// - if overlay mode leaves a deck column's or the wave stack's border painted
//   then hiding the UI leaves a wireframe of rules behind

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relative) {
	return readFileSync(fileURLToPath(new URL(`../../src/${relative}`, import.meta.url)), 'utf8');
}

const APP_HTML = source('app.html');
const APP_CSS = source('app.css');
const PERFORMANCE_PAGE = source('routes/performance/+page.svelte');

test('app.html paints the app background before any stylesheet loads', () => {
	const head = APP_HTML.slice(APP_HTML.indexOf('<head>'), APP_HTML.indexOf('</head>'));
	const bootStyle = head.match(/<style>\s*html\s*\{([^}]*)\}\s*<\/style>/);
	assert.ok(bootStyle, 'the head must carry an inline html{} rule');
	// An invariant, not a value: whatever --bg is, the boot rule matches it.
	const appBackground = APP_CSS.match(/:root\s*\{[^}]*?--bg:\s*(#[0-9a-fA-F]{6});/);
	assert.ok(appBackground, 'app.css must declare --bg as a six-digit hex');
	const bootBackground = bootStyle[1].match(/background:\s*(#[0-9a-fA-F]{6});/);
	assert.ok(bootBackground, 'the boot rule must set a six-digit hex background');
	assert.equal(bootBackground[1].toLowerCase(), appBackground[1].toLowerCase());
	// The inline rule sits ahead of SvelteKit's own head output, so it is in
	// force for the first paint and loses to every later rule of equal weight.
	assert.ok(head.indexOf('<style>') < head.indexOf('%sveltekit.head%'));
});

test('the boot rule is a bare element selector, so overlay mode can still clear the background', () => {
	// html.tw-desktop-passthrough { background: transparent } has to keep
	// winning; an id, a class or !important on the boot rule would beat it.
	const bootStyle = APP_HTML.match(/<style>([\s\S]*?)<\/style>/);
	assert.ok(bootStyle);
	assert.doesNotMatch(bootStyle[1], /!important/);
	assert.match(bootStyle[1], /^\s*html\s*\{/);
	assert.match(PERFORMANCE_PAGE, /:global\(html\.tw-desktop-passthrough\)/);
});

test('overlay mode clears the separator rules of each hidden region', () => {
	// The three rules that outlived their regions: both deck columns' inner
	// borders and the wave stack's bottom border.
	const rule = PERFORMANCE_PAGE.match(
		/\.perf-root\.tw-active:not\(\.tw-left-visible\) \.deck-col:first-child,\s*\n\s*\.perf-root\.tw-active:not\(\.tw-right-visible\) \.deck-col:last-child,\s*\n\s*\.perf-root\.tw-active:not\(\.tw-top-visible\) :global\(\.rb-wavestack\) \{\s*\n\s*border-color: transparent;/
	);
	assert.ok(rule, 'tw-active must clear the deck-col and wavestack borders per hidden edge');
	// Opposite direction: outside overlay mode the deck columns keep their rules.
	assert.match(PERFORMANCE_PAGE, /\.deck-col:first-child \{\s*\n\s*grid-area: decks-left;\s*\n\s*border-right: 1px solid #3d4652;/);
	assert.match(PERFORMANCE_PAGE, /\.deck-col:last-child \{\s*\n\s*grid-area: decks-right;\s*\n\s*border-left: 1px solid #3d4652;/);
});
