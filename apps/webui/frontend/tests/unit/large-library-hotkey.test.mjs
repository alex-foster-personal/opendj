// requirement: PARITY-13833
// [if] right Option does not toggle the large library, or left Option / typing triggers it [then] the library hotkey misfires, [else stop].
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';

import { loadTypeScriptModule } from './load-typescript.mjs';

const H = await loadTypeScriptModule('src/lib/rb/large-library-hotkey.ts');
const ev = (over = {}) => ({
	code: 'AltRight',
	metaKey: false,
	ctrlKey: false,
	shiftKey: false,
	target: null,
	...over
});

test('AltRight alone toggles; AltLeft and chords do not', () => {
	assert.equal(H.isLargeLibraryHotkey(ev()), true);
	assert.equal(H.isLargeLibraryHotkey(ev({ code: 'AltLeft' })), false);
	assert.equal(H.isLargeLibraryHotkey(ev({ metaKey: true })), false);
	assert.equal(H.isLargeLibraryHotkey(ev({ repeat: true })), false);
});

test('AltRight inside an input does nothing', () => {
	const input = {
		tagName: 'INPUT',
		type: 'text',
		isContentEditable: false,
		closest: () => null,
		getAttribute: () => null
	};
	assert.equal(H.isLargeLibraryHotkey(ev({ target: input })), false);
});

test('the technically-working Opt reveal ignores AltRight', () => {
	const src = readFileSync(
		new URL('../../src/lib/rb/technically-working-hotkeys.ts', import.meta.url),
		'utf8'
	);
	assert.match(src, /e\.key === 'Alt' && e\.code !== 'AltRight'/);
});
