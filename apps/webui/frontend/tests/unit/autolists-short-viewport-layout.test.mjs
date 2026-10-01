// requirement: PERF-UI-01
// [if] Autolists at 720p [then] body flex splits browser and smartlists [else stop].

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const LIBRARY_NAV_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/LibraryNav.svelte', import.meta.url)
);

test('LibraryNav wraps autolist browser and smartlist scroll in autolists-body', () => {
	const src = readFileSync(LIBRARY_NAV_PATH, 'utf8');
	assert.match(src, /class="autolists-body"/);
	assert.match(src, /data-testid="autolists-body"/);
	assert.match(src, /class="autolist-browser-wrap"[\s\S]*class="autolists-scroll"/);
	assert.match(src, /data-testid="autolists-scroll"/);
});

test('LibraryNav autolists flex CSS reserves min-height for smartlist scroll', () => {
	const src = readFileSync(LIBRARY_NAV_PATH, 'utf8');
	assert.match(src, /\.autolists-body\s*\{[^}]*flex:\s*1\s+1\s+0/);
	assert.match(src, /\.autolists-body\s*\{[^}]*min-height:\s*0/);
	assert.match(src, /\.autolists-body\s*\{[^}]*flex-direction:\s*column/);
	assert.match(src, /\.autolist-browser-wrap\s*\{[^}]*max-height:\s*50%/);
	assert.match(src, /\.autolist-browser-wrap\s*\{[^}]*min-height:\s*0/);
	const minH = src.match(/\.autolists-scroll\s*\{[^}]*min-height:\s*(\d+)px/);
	assert.ok(minH, 'expected .autolists-scroll min-height');
	assert.ok(Number(minH[1]) >= 40, 'smartlist scroll min-height must be at least 40px');
});
