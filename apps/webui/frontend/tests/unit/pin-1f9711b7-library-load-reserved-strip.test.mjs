/**
 * pins 1f9711b7, dd5fad7f, e452be6b: the library loading indicator covered the
 * first track rows (measured top 548px against a first row at 546px) because
 * it was an absolutely positioned overlay inside the table body.
 *
 * It then owned a RESERVED 18px strip between the browser toolbar and the
 * column headers, which left an empty band there whenever nothing was loading
 * (W16, LIBUX-50: "loading bar space is still there when not loading"). Now
 * the root is a zero-height anchor directly above the table, always mounted,
 * and the strip is an absolutely positioned child of it. So the strip still
 * sits at the table's top edge (the header row), never inside the table body,
 * and neither appearing nor disappearing moves the list.
 *
 * - if the strip root is conditional then the role=status live region is
 *   torn down between loads -> broken
 * - if the root takes height again then idle shows an empty band -> broken
 * - if the strip is in normal flow then the headers move on every load start
 *   and end (pin 02717d4ea496) -> broken
 * - if BrowserPanel hands the indicator to TrackTable's bodyOverlay again
 *   then it paints over the first rows -> broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const indicatorPath = path.join(
	here,
	'../../src/lib/components/rb/browser/LibraryLoadIndicator.svelte'
);
const browserPanelPath = path.join(here, '../../src/lib/components/rb/BrowserPanel.svelte');

function stripComments(src) {
	return src
		.replace(/<!--[\s\S]*?-->/g, '')
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.replace(/\/\/.*$/gm, '');
}

function cssRule(src, selector) {
	const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	const match = new RegExp(`(?:^|\\n)\\t${escaped} \\{([\\s\\S]*?)\\n\\t\\}`).exec(src);
	assert.ok(match, `expected a ${selector} rule`);
	return match[1];
}

test('the load strip root is always mounted, only its content is conditional', () => {
	const src = stripComments(readFileSync(indicatorPath, 'utf8'));
	const markup = src.slice(src.indexOf('</script>'), src.indexOf('<style>'));
	const rootAt = markup.indexOf('class="lli-root"');
	const firstIfAt = markup.indexOf('{#if');
	assert.ok(rootAt !== -1, 'expected the lli-root element');
	assert.ok(firstIfAt !== -1, 'expected a conditional around the strip content');
	assert.ok(
		rootAt < firstIfAt,
		'lli-root must sit OUTSIDE the loading conditional so the status region stays mounted'
	);
	assert.match(
		markup.slice(rootAt, markup.length),
		/\{#if loading \|\| progress !== null \|\| searching\}\s*<div class="lli-strip">/,
		'the strip must still be gated on a real in-flight load'
	);
});

test('the root reserves no height and the strip is a fixed-height overlay on it', () => {
	const src = stripComments(readFileSync(indicatorPath, 'utf8'));
	const root = cssRule(src, '.lli-root');
	assert.match(root, /flex: 0 0 0;/, 'the root must not flex with the table');
	assert.match(root, /height: 0;/, 'the root must reserve no height');
	assert.match(root, /position: relative;/, 'the root anchors the overlay strip');
	assert.match(root, /--lli-strip-h: 18px;/, 'the strip height must be declared once');
	const strip = cssRule(src, '.lli-strip');
	assert.match(strip, /position: absolute;/, 'the strip must be out of flow');
	assert.match(strip, /inset: 0 0 auto 0;/, 'the strip must hang DOWN from the table top edge');
	assert.match(strip, /height: var\(--lli-strip-h\);/, 'the strip height must be fixed');
	assert.match(strip, /overflow: hidden;/, 'a long label must clip, not grow the strip');
	const mark = cssRule(src, '.lli-mark');
	const markHeight = /height: (\d+)px;/.exec(mark);
	assert.ok(markHeight, 'expected the mark to declare a pixel height');
	assert.ok(Number(markHeight[1]) <= 18, 'the mark must fit inside the strip');
});

test('BrowserPanel mounts the strip above the table instead of as a body overlay', () => {
	const panel = stripComments(readFileSync(browserPanelPath, 'utf8'));
	assert.ok(
		!/bodyOverlay=/.test(panel),
		'the indicator must no longer be handed to TrackTable as an overlay on its rows'
	);
	assert.match(
		panel,
		/<LibraryLoadIndicator[^>]*\/>\s*<TrackTable/,
		'the reserved strip must sit directly above the table, below the toolbar'
	);
	const mounts = panel.match(/<LibraryLoadIndicator/g) ?? [];
	assert.equal(mounts.length, 1, 'exactly one load strip per browser pane');
});
