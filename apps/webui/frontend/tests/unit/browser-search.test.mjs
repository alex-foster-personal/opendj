import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('text command search publishes the query to browser subscribers', async () => {
	const search = await loadTypeScriptModule('src/lib/rb/browser-search.ts');
	const received = [];
	const unsubscribe = search.subscribeBrowserSearch((request) => received.push(request));

	search.requestBrowserSearch('  daft punk  ');
	unsubscribe();
	assert.throws(
		() => search.requestBrowserSearch('ignored after unsubscribe'),
		/no mounted result reporter/
	);

	assert.deepEqual(received, [
		{ query: '', revision: 0, result: null },
		{ query: 'daft punk', revision: 1, result: null }
	]);
});

test('blank browser searches fail fast', async () => {
	const search = await loadTypeScriptModule('src/lib/rb/browser-search.ts');

	assert.throws(() => search.requestBrowserSearch('   '), /non-empty/);
});

test('search commands carry the filter-bypass result reported by the browser', async () => {
	const search = await loadTypeScriptModule('src/lib/rb/browser-search.ts');
	const unsubscribe = search.subscribeBrowserSearch(() => {});
	const request = search.requestBrowserSearch('azara');

	search.reportBrowserSearchResult(request, {
		fallback: true,
		ignoredFilters: ['next-only'],
		rowCount: 2
	});

	assert.deepEqual(request.result, {
		fallback: true,
		ignoredFilters: ['next-only'],
		rowCount: 2
	});
	unsubscribe();
});

test('browser searches fail explicitly when no mounted browser can report their result', async () => {
	const search = await loadTypeScriptModule('src/lib/rb/browser-search.ts');
	assert.throws(() => search.requestBrowserSearch('azara'), /no mounted result reporter/);
});

test('a superseded search cannot report over the command that replaced it', async () => {
	const search = await loadTypeScriptModule('src/lib/rb/browser-search.ts');
	const unsubscribe = search.subscribeBrowserSearch(() => {});

	// Same query twice is the reachable case: the first FTS is still in flight
	// when the second command lands, and the guard in _searchWholeCollection
	// keys off the query text, so it does NOT suppress the first one's report.
	const superseded = search.requestBrowserSearch('azara');
	const current = search.requestBrowserSearch('azara');

	assert.equal(search.isCurrentBrowserSearch(superseded), false);
	assert.equal(search.isCurrentBrowserSearch(current), true);
	// Without the isCurrentBrowserSearch guard in BrowserPanel this throw
	// happens inside a `void`-ed promise, i.e. an unhandled rejection.
	assert.throws(
		() => search.reportBrowserSearchResult(superseded, {
			fallback: false,
			ignoredFilters: [],
			rowCount: 0
		}),
		/stale/
	);

	search.reportBrowserSearchResult(current, {
		fallback: false,
		ignoredFilters: [],
		rowCount: 4
	});
	assert.equal(current.result.rowCount, 4);
	unsubscribe();
});

// pin 5e3ed689ad3a: this asserted the OPPOSITE placement - the toggle inside
// `.search-options`, which renders only while the search box is focused. That
// is exactly the bug the maintainer reported ("where has the checkbox gone?"), so the
// assertions are inverted rather than dropped: the control must now sit in the
// always-rendered header row, outside the search-focus block, and must NOT be
// inside `.search-options` any more. The real-browser proof that it is visible
// without touching search lives in performance-library-panels.spec.ts.
test('the compatible filter is always reachable in the header, never gated on search focus', async () => {
	const source = await import('node:fs/promises').then(({ readFile }) =>
		readFile(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url), 'utf8')
	);
	const searchOnlyBlock = source.slice(source.indexOf('class="search-options"'));
	assert.ok(
		!/class="search-options"[\s\S]*?class="next-only"[\s\S]*?<SearchBox/.test(source),
		'the compatible toggle must not be inside the search-focused options row'
	);
	assert.ok(
		searchOnlyBlock.includes('class="whole-collection"'),
		'Search all playlists stays in the search-focused options row'
	);
	assert.match(source, /class="hide-broken"[\s\S]*?class="next-only"[\s\S]*?<span>compatible<\/span>/);
	// One explainer per control (#5535): the description lives in the hover
	// card, not in a title= on the label that would draw a second box.
	assert.doesNotMatch(source, /class="next-only"\s+title=/);
	const panel = await import('node:fs/promises').then(({ readFile }) =>
		readFile(new URL('../../src/lib/components/rb/browser/CompatibleFilterPanel.svelte', import.meta.url), 'utf8')
	);
	assert.match(
		panel.replace(/\s+/g, ' '),
		/Show only tracks compatible with the reference deck \(master, else playing, else any loaded with key and BPM\): Camelot key family/
	);
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

test('browser labels filter-bypass rows and command output reports the bypass', async () => {
	const read = (path) =>
		import('node:fs/promises').then(({ readFile }) =>
			readFile(new URL(`../../src/lib/components/rb/${path}`, import.meta.url), 'utf8')
		);
	const [panel, commandEntry] = await Promise.all([
		read('BrowserPanel.svelte'),
		read('CommandEntry.svelte')
	]);

	assert.match(panel, /resolveSearchFilterFallback\(/);
	assert.match(panel, /pane\.search\.trim\(\) === '' \? \[\] : _searchFilterNames\(true\)/);
	assert.match(panel, /fetched by ignoring the active/);
	assert.match(panel, /class="filter-fallback-note"/);
	assert.match(panel, /reportBrowserSearchResult\(request,/);
	assert.match(commandEntry, /browserSearchResult === null/);
	assert.match(commandEntry, /subscribeBrowserSearchResult/);
});

test('command searches report only once the collection search they asked for has landed', async () => {
	const panel = await import('node:fs/promises').then(({ readFile }) =>
		readFile(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url), 'utf8')
	);

	// Regression (PR #1281 review): the subscriber used to call _setSearchNow
	// and report in the same tick. Under Search all playlists the report reads
	// pane.search_results, which only fills when the server FTS returns, so the
	// command received the PREVIOUS query's fallback state and row count and
	// was never corrected. The request must ride into _searchWholeCollection
	// and be reported from its completion path instead.
	assert.match(panel, /void _searchWholeCollection\(active, next, request\);/);
	assert.match(
		panel,
		/active\.searching = false;\n\t+if \(request !== undefined && active === pane\) _reportBrowserSearchResult\(request\);/,
		'the whole-collection report must fire from the search-completion path'
	);
	// A superseded FTS must drop its answer rather than throw on the stale
	// guard inside an unawaited promise.
	assert.match(panel, /if \(!isCurrentBrowserSearch\(request\)\) return;/);
	// Programmatic writes must still cancel the pending burst FIRST, or the
	// burst overwrites them (library-perf-wiring asserts the same pair).
	assert.match(
		panel,
		/_filterDebounceFor\(activePane\)\.cancel\(\);\n\t+panes\[activePane\]\.setSearch\(next\);/
	);
});
