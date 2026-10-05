/**
 * Issue #2071: KEY SYNC sits immediately right of the deck-header key badge
 * in the chrome row, before BEAT SYNC / MASTER, with nowrap so transport
 * controls do not wrap at 1280px.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const header = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/deck/DeckHeader.svelte', import.meta.url)),
	'utf8'
);

function chromeBlock() {
	const anchor = header.indexOf('class="chrome"');
	assert.notEqual(anchor, -1, 'no .chrome block in DeckHeader');
	const open = header.lastIndexOf('<div', anchor);
	assert.notEqual(open, -1, 'no opening div for .chrome');
	const close = header.indexOf('</div>', header.indexOf('class="sync-col"'));
	assert.notEqual(close, -1, '.chrome block never closed');
	return header.slice(open, close + '</div>'.length);
}

test('if deck header chrome renders then KEY SYNC DOM order immediately follows key badge before sync-col', () => {
	const block = chromeBlock();
	const keyBadgeIdx = block.indexOf('class="key-badge"');
	const keySyncIdx = block.indexOf('data-testid={`key-sync-deck-${deckId}`}');
	const syncColIdx = block.indexOf('class="sync-col"');
	assert.notEqual(keyBadgeIdx, -1, 'no key-badge in .chrome');
	assert.notEqual(keySyncIdx, -1, 'no KEY SYNC button in .chrome');
	assert.notEqual(syncColIdx, -1, 'no sync-col in .chrome');
	assert.ok(keyBadgeIdx < keySyncIdx, 'if key badge renders then KEY SYNC must follow it');
	assert.ok(keySyncIdx < syncColIdx, 'if KEY SYNC renders then sync-col must follow it');
	const between = block.slice(keyBadgeIdx, keySyncIdx);
	assert.equal(
		between.indexOf('class="sync-col"'),
		-1,
		'if KEY SYNC sits beside key readout then transport controls must not sit between them'
	);
});

test('if KEY SYNC is clicked then behavior stays on the existing onKeySync IPC path', () => {
	const block = chromeBlock();
	const keySyncIdx = block.indexOf('class="rb-lit-button keysync"');
	assert.notEqual(keySyncIdx, -1, 'no KEY SYNC button in .chrome');
	const buttonEnd = block.indexOf('</button>', keySyncIdx);
	assert.notEqual(buttonEnd, -1, 'KEY SYNC button never closed');
	const keySyncSlice = block.slice(keySyncIdx, buttonEnd);
	assert.match(
		keySyncSlice,
		/onclick=\{async \(\) => await onKeySync\(\)\}/,
		'if KEY SYNC is clicked then it must still dispatch through onKeySync'
	);
});

test('if deck header renders on 1280px width then chrome nowrap keeps transport controls on one row', () => {
	assert.match(
		header,
		/\.chrome\s*\{[^}]*flex-wrap:\s*nowrap/,
		'if deck header renders on 1280px width then KEY SYNC must not wrap transport controls off the chrome row'
	);
});

test('if KEY SYNC is armed but not following then the button is not lit (DECKUX-34)', () => {
	const block = chromeBlock();
	const keySyncIdx = block.indexOf('class="rb-lit-button keysync"');
	const keySyncSlice = block.slice(keySyncIdx, block.indexOf('</button>', keySyncIdx));
	assert.match(
		keySyncSlice,
		/class:lit=\{keySyncState === 'following'\}/,
		'if KEY SYNC lights from the arm instead of the follow status then it can show ON over a wrong key'
	);
	assert.doesNotMatch(
		keySyncSlice,
		/class:lit=\{deck\.key_sync_enabled\}/,
		'if KEY SYNC lights from key_sync_enabled then a waiting arm reads as following'
	);
	assert.match(
		keySyncSlice,
		/disabled=\{pending \|\| \(!keySyncAvailable && !deck\.key_sync_enabled\)\}/,
		'if an armed KEY SYNC with no master is disabled then the operator cannot disarm it'
	);
});
