import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

test('TreeContextMenu exposes New smartlist and smartlist Rename/Duplicate', () => {
	const src = source('src/lib/components/rb/browser/TreeContextMenu.svelte');
	const playlistIdx = src.indexOf("'New playlist'");
	const smartlistIdx = src.indexOf("'New smartlist'");
	assert.ok(playlistIdx >= 0 && smartlistIdx > playlistIdx);
	assert.match(src, /onrenamesmartlist\?\.\(smartlist\)/);
	assert.match(src, /onduplicatesmartlist\?\.\(smartlist\)/);
});

test('TreeSmartlistSection supports inline rename and duplicate/create flows', () => {
	const src = source('src/lib/components/rb/browser/TreeSmartlistSection.svelte');
	assert.match(src, /aria-label="Rename smartlist"/);
	assert.match(src, /createAndRename/);
	assert.match(src, /Duplicated as/);
});

test('LibraryNav passes oncreatesmartlist into PlaylistTree', () => {
	const src = source('src/lib/components/rb/browser/LibraryNav.svelte');
	assert.match(src, /oncreatesmartlist/);
	assert.match(src, /PlaylistTree/);
});

test('PlaylistTree still does not inline smartlists rows', () => {
	const src = source('src/lib/components/rb/browser/PlaylistTree.svelte');
	assert.doesNotMatch(src, /\{#each smartlists\.rows/);
	assert.doesNotMatch(src, /TreeSmartlistSection/);
});
