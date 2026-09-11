import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url), 'utf8');

test('library horizontal scrollbar is a two-pixel bar', () => {
	assert.ok(/\.table-wrap::-webkit-scrollbar\s*\{\s*height: 2px;\s*\}/.test(source), 'horizontal scrollbar must be 2px');
	assert.ok(/\.table-wrap::-webkit-scrollbar-thumb:horizontal\s*\{[^}]*background: var\(--rb-text-dim\);/.test(source), 'horizontal thumb must remain visible');
	assert.ok(/\.table-wrap::-webkit-scrollbar-button:horizontal\s*\{\s*display: none;\s*\}/.test(source), 'horizontal scrollbar has no arrow buttons');
});

test('library keeps native scrolling and does not resize the vertical scrollbar', () => {
	assert.ok(/\.table-wrap\s*\{[^}]*overflow: auto;/.test(source), 'native scrolling must remain enabled');
	const scrollbar = source.match(/\.table-wrap::-webkit-scrollbar\s*\{([^}]*)\}/)?.[1];
	assert.ok(scrollbar, 'horizontal scrollbar rule missing');
	assert.doesNotMatch(scrollbar, /\bwidth\s*:/);
});
