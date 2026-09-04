import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('text command search publishes the query to browser subscribers', async () => {
	const search = await loadTypeScriptModule('src/lib/rb/browser-search.ts');
	const received = [];
	const unsubscribe = search.subscribeBrowserSearch((request) => received.push(request));

	search.requestBrowserSearch('  daft punk  ');
	unsubscribe();
	search.requestBrowserSearch('ignored after unsubscribe');

	assert.deepEqual(received, [
		{ query: '', revision: 0 },
		{ query: 'daft punk', revision: 1 }
	]);
});

test('blank browser searches fail fast', async () => {
	const search = await loadTypeScriptModule('src/lib/rb/browser-search.ts');

	assert.throws(() => search.requestBrowserSearch('   '), /non-empty/);
});

test('Next-only moves into the compact search stack above SearchBox while searching', async () => {
	const source = await import('node:fs/promises').then(({ readFile }) =>
		readFile(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url), 'utf8')
	);
	assert.match(source, /\{#if searchFocused \|\| pane\.search\.trim\(\) !== ''\}/);
	assert.match(source, /class="search-options"[\s\S]*?class="next-only"[\s\S]*?<SearchBox/);
	assert.match(source, /title="Filter visible candidates by Camelot and BPM\. Shortcut: Tab"/);
});

test('Search all playlists explains its current-pane and collection scopes', async () => {
	const source = await import('node:fs/promises').then(({ readFile }) =>
		readFile(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url), 'utf8')
	);
	assert.match(source, /<span>Search all playlists<\/span>/);
	assert.match(
		source,
		/title="Search all playlists uses server FTS across the collection\. Unchecked filters only the current pane\."/
	);
});
