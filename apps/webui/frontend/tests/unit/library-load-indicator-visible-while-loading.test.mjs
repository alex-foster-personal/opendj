import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const componentsDir = path.join(__dirname, '../../src/lib/components/rb/browser');
const browserPanelPath = path.join(__dirname, '../../src/lib/components/rb/BrowserPanel.svelte');

/** Strip line + block comments before scanning for real code, same
 * convention as analysis-dots-popover.test.mjs / pairings-inert.test.mjs -
 * doc-comment prose must never false-positive as the real render guard. */
function stripComments(src) {
	return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
}

// Regression: pin 0e8d6e095cd5 (follow-on to ad59ac047429). The loading
// indicator existed in source but was invisible on screen because
// LibraryLoadIndicator's root gate was `progress !== null`, while
// PaneStore.beginLoad sets load_progress back to null for the WHOLE
// duration of a load until the first page callback fires (all_tracks) or
// forever (single-playlist loads pass no onPage at all - see _loadPane in
// BrowserPanel.svelte). So during the entire "loading..." text state the
// indicator rendered nothing: no mark, no bar.
//
// - if the root gate still reads only `progress !== null` (no `loading`
//   term) then a cold load shows bare "loading..." text with no indicator,
//   reproducing the pin, until the first page of All Tracks lands (or
//   never, for an ordinary playlist load) -> broken.
// - if BrowserPanel doesn't pass pane.loading through to the component then
//   the component has no way to know a load is in flight when progress is
//   still null -> broken.
// - if the label has no indeterminate ("Loading" with no numbers) branch for
//   progress === null then the component either renders nothing or throws
//   dereferencing progress.loaded on a null progress -> broken.

test('LibraryLoadIndicator renders while loading even before progress is known', () => {
	const src = stripComments(
		readFileSync(path.join(componentsDir, 'LibraryLoadIndicator.svelte'), 'utf8')
	);
	assert.match(
		src,
		/\{#if\s+[^}]*\bloading\b[^}]*\}/,
		'root render guard must reference a `loading` flag, not just `progress !== null`, ' +
			'so the loading indicator is visible for the whole loading state, not only once the first ' +
			'page callback has fired'
	);
});

test('LibraryLoadIndicator has an indeterminate label when progress is still null', () => {
	const src = stripComments(
		readFileSync(path.join(componentsDir, 'LibraryLoadIndicator.svelte'), 'utf8')
	);
	assert.match(
		src,
		/progress\s*===\s*null/,
		'must branch on progress === null so it never dereferences progress.loaded on a null progress'
	);
});

test('BrowserPanel passes pane.loading through to LibraryLoadIndicator', () => {
	const src = stripComments(readFileSync(browserPanelPath, 'utf8'));
	assert.match(
		src,
		/<LibraryLoadIndicator[^>]*\bloading=\{[^}]*pane\.loading[^}]*\}/,
		'BrowserPanel must wire pane.loading into LibraryLoadIndicator so it can render before ' +
			'the first page of progress is known'
	);
});

test('LibraryLoadIndicator keeps an honest visible track for determinate and indeterminate loads', () => {
	const src = stripComments(
		readFileSync(path.join(componentsDir, 'LibraryLoadIndicator.svelte'), 'utf8')
	);
	assert.match(src, /class="lli-track"/, 'every loading state needs a visible progress track');
	assert.match(
		src,
		/class:lli-indeterminate=\{pct === null\}/,
		'a missing denominator must render an indeterminate state, not omit the bar'
	);
	assert.match(src, /outline: 1px solid/, 'the track endpoint must remain visible');
	assert.match(src, /height: 2px/, 'the bar must match the 2px vocal-bar height');
	assert.match(src, /#4fb2ff/, 'the bar must use the vocal-bar blue');
});

test('LibraryLoadIndicator uses a delayed monochrome oDj mark so fast loads do not flash it', () => {
	const src = stripComments(
		readFileSync(path.join(componentsDir, 'LibraryLoadIndicator.svelte'), 'utf8')
	);
	assert.match(src, /mask: url\('\/favicon\.svg'\)/, 'the loading mark must reuse the oDj icon');
	assert.match(src, /library-mark-reveal 180ms step-end both/, 'the mark must wait briefly before appearing');
	assert.match(src, /library-mark-spin 420ms linear infinite/, 'the visible mark must spin quickly');
});
