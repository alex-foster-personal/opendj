import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

test('TreeContextMenu exposes smartlist context menu with Delete enabled', () => {
	const src = source('src/lib/components/rb/browser/TreeContextMenu.svelte');
	assert.match(src, /export function openSmartlist/);
	assert.match(src, /ondeletesmartlist\?\.\(smartlist\)/);
});

test('TreeSmartlistSection wires smartlist rows for delete', () => {
	const src = source('src/lib/components/rb/browser/TreeSmartlistSection.svelte');
	assert.match(src, /data-testid="smartlist-row"/);
	assert.match(src, /oncontextmenu/);
	assert.match(src, /Delete smartlist/);
});

test('tree-smartlists removes deleted rows locally', () => {
	const src = source('src/lib/components/rb/browser/tree-smartlists.svelte.ts');
	assert.match(src, /deleteSmartlist/);
	assert.match(src, /filter\(\(row\) => row\.id !== id\)/);
});

test('PlaylistTree no longer inlines the smartlists each loop', () => {
	const src = source('src/lib/components/rb/browser/PlaylistTree.svelte');
	assert.doesNotMatch(src, /\{#each smartlists\.rows/);
	assert.doesNotMatch(src, /TreeSmartlistSection/);
});

test('LibraryNav mounts TreeSmartlistSection on the Autolists tab', () => {
	const src = source('src/lib/components/rb/browser/LibraryNav.svelte');
	assert.match(src, /TreeSmartlistSection/);
});
