/**
 * LIBM-175 (the maintainer, Tue 6 Oct 2026): "Search pre-fill should just say
 * 'Search...' the whole collection button makes obvious the scope".
 *
 * @pytest.mark.requirement LIBM-175
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const PANEL = readFileSync('src/lib/components/rb/BrowserPanel.svelte', 'utf8');
const BOX = readFileSync('src/lib/components/rb/browser/SearchBox.svelte', 'utf8');

let mod;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/browser-search-query.ts');
});

test('if the search placeholder is not exactly "Search..." then the box still spells out its scope', () => {
	assert.equal(mod.SEARCH_PLACEHOLDER, 'Search...');
});

test('if list or whole-collection mode reads anything but SEARCH_PLACEHOLDER then the old scope text is back', () => {
	const derived = PANEL.match(/const searchPlaceholder = \$derived\(([\s\S]*?)\);/);
	assert.ok(derived, 'expected the searchPlaceholder $derived in BrowserPanel.svelte');
	// Find mode is the one exception: it highlights, so it keeps its own text (control).
	// Every other mode passes undefined, so SearchBox's default applies.
	assert.match(derived[1], /searchMode === 'find' \? 'Find in list \(highlight, 3\+ chars\)' : undefined/);
	assert.match(PANEL, /placeholder=\{searchPlaceholder\}/);
	assert.match(BOX, /placeholder = SEARCH_PLACEHOLDER,/);
	for (const src of [PANEL, BOX]) {
		assert.doesNotMatch(src, /'Search within this track list'/);
		assert.doesNotMatch(src, /'Search whole collection'/);
	}
});
