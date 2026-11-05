/**
 * PLAYLIST-ROW-01: clicking the library undo button must not open the edit
 * history popover over the playlist rows; only hover intent or keyboard focus.
 *
 * [if] the undo button opens the history on any focus, so a mouse click opens it [then ⛔]
 * [if] keyboard focus (:focus-visible) no longer opens the history at once [then ⛔]
 * [if] hover intent no longer opens the history [then ⛔]
 */
// requirement: PLAYLIST-ROW-01
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const SOURCE = readFileSync(
	new URL('../../src/lib/components/rb/browser/LibrarySourceTabs.svelte', import.meta.url),
	'utf8'
);

function _undoButtonMarkup() {
	const start = SOURCE.indexOf('data-testid="playlist-undo"');
	assert.ok(start > 0, 'undo button markup not found');
	return SOURCE.slice(start, SOURCE.indexOf('</button>', start));
}

test('if the undo button opens the history on any focus (mouse click) then broken', () => {
	const markup = _undoButtonMarkup();
	assert.doesNotMatch(markup, /onfocus=\{openHistoryNow\}/, 'click-focus must not open the popover');
	assert.match(markup, /onfocus=\{openHistoryOnKeyboardFocus\}/);
});

test('if keyboard focus no longer opens the history at once then broken', () => {
	const body = SOURCE.slice(SOURCE.indexOf('function openHistoryOnKeyboardFocus'));
	const fn = body.slice(0, body.indexOf('\n\t}\n'));
	assert.match(fn, /matches\(':focus-visible'\)/);
	assert.match(fn, /openHistoryNow\(\)/);
});

test('if hover intent no longer opens the history then broken', () => {
	assert.match(_undoButtonMarkup(), /onmouseenter=\{intendHistory\}/);
});
