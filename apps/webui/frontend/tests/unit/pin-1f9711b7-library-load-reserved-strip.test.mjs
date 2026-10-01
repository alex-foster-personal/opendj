/**
 * pins 1f9711b7, dd5fad7f, e452be6b: the library loading indicator covered the
 * first track rows (measured top 548px against a first row at 546px) because
 * it was an absolutely positioned overlay inside the table body.
 *
 * It now owns a RESERVED strip between the browser toolbar and the column
 * headers. The strip is always in the layout at one fixed height, so it can
 * never cover a row and the list cannot jump when a load starts or ends. That
 * second half is what the earlier sibling-above-the-table attempt got wrong
 * (pin 02717d4ea496): it only took space while loading, so the headers moved.
 *
 * - if the strip root is conditional then the headers and rows shift on every
 *   load start and end -> broken
 * - if the strip has no fixed height then a longer label grows it and the
 *   list jumps -> broken
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

test('the load strip root is always in the layout, only its content is conditional', () => {
	const src = stripComments(readFileSync(indicatorPath, 'utf8'));
	const markup = src.slice(src.indexOf('</script>'), src.indexOf('<style>'));
	const rootAt = markup.indexOf('class="lli-root"');
	const firstIfAt = markup.indexOf('{#if');
	assert.ok(rootAt !== -1, 'expected the lli-root element');
	assert.ok(firstIfAt !== -1, 'expected a conditional around the strip content');
	assert.ok(
		rootAt < firstIfAt,
		'lli-root must sit OUTSIDE the loading conditional so its space is reserved while idle'
	);
	assert.match(
		markup.slice(rootAt, markup.length),
		/\{#if loading \|\| progress !== null \|\| searching\}/,
		'the strip content must still be gated on a real in-flight load'
	);
});

test('the load strip has one fixed height and is never positioned over the rows', () => {
	const src = stripComments(readFileSync(indicatorPath, 'utf8'));
	const root = cssRule(src, '.lli-root');
	assert.match(root, /flex: 0 0 var\(--lli-strip-h\);/, 'the strip must not flex with the table');
	assert.match(root, /height: var\(--lli-strip-h\);/, 'the strip height must be fixed');
	assert.match(root, /--lli-strip-h: 18px;/, 'the reserved height must be declared once');
	assert.match(root, /overflow: hidden;/, 'a long label must clip, not grow the strip');
	assert.ok(!/position:\s*(absolute|fixed)/.test(root), 'the strip must be in normal flow');
	const mark = cssRule(src, '.lli-mark');
	const markHeight = /height: (\d+)px;/.exec(mark);
	assert.ok(markHeight, 'expected the mark to declare a pixel height');
	assert.ok(Number(markHeight[1]) <= 18, 'the mark must fit inside the reserved strip');
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
