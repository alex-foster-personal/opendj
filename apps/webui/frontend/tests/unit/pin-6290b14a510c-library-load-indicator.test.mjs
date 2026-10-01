/**
 * pin 6290b14a510c: library loading chrome with spinning monochrome oDj mark,
 * vocal-blue 2px progress bar with a visible end, honest progress (LIBUX-10).
 *
 * [if] loading state hides the indicator until progress is known [then STOP]
 * [if] the bar is not vocal blue / 2px / favicon mask spin [then STOP]
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const componentsDir = path.join(__dirname, '../../src/lib/components/rb/browser');
const browserPanelPath = path.join(__dirname, '../../src/lib/components/rb/BrowserPanel.svelte');

function stripComments(src) {
	return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
}

test('pin 6290b14a510c: LibraryLoadIndicator shows mark, bar, and indeterminate progress while loading', () => {
	const src = stripComments(
		readFileSync(path.join(componentsDir, 'LibraryLoadIndicator.svelte'), 'utf8')
	);
	assert.match(
		src,
		/\{#if\s+[^}]*\bloading\b[^}]*\}/,
		'root guard must include loading so the overlay is visible before the first page callback'
	);
	assert.match(src, /progress\s*===\s*null/, 'must branch when progress is still null');
	assert.match(src, /class:lli-indeterminate=\{pct === null\}/, 'null denominator uses indeterminate sweep');
	assert.match(src, /class="lli-track"/, 'visible progress track');
	assert.match(src, /height: 2px/, 'bar height matches vocal bars');
	assert.match(src, /#4fb2ff/, 'bar uses vocal-bar blue');
	assert.match(src, /outline: 1px solid/, 'track end must stay visible');
	assert.match(src, /mask: url\('\/favicon\.svg'\)/, 'monochrome spinning oDj icon');
	assert.match(src, /library-mark-reveal 180ms step-end both/, 'mark waits briefly before appearing');
	assert.match(src, /library-mark-spin 420ms linear infinite/, 'mark spins quickly once visible');
});

test('pin 6290b14a510c: BrowserPanel wires pane.loading into LibraryLoadIndicator', () => {
	const src = stripComments(readFileSync(browserPanelPath, 'utf8'));
	assert.match(
		src,
		/<LibraryLoadIndicator[^>]*\bloading=\{[^}]*pane\.loading[^}]*\}/,
		'loading flag must reach the indicator while progress is still null'
	);
});
