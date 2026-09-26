// Whole-collection search failure empty-state (issue #2449, LIBMX-05).
// - [if] a whole-collection search fails at the network/API layer [then] the
//   empty-state message says the search failed, not that nothing matched
// - [if] the failed-search message is shown [then] it offers a retry action

import assert from 'node:assert/strict';
import { before, test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import { loadTypeScriptModule } from './load-typescript.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const browserPanelPath = path.join(__dirname, '../../src/lib/components/rb/BrowserPanel.svelte');
const trackTablePath = path.join(
	__dirname,
	'../../src/lib/components/rb/browser/TrackTable.svelte'
);

/** Strip line + block comments before scanning for real code. */
function stripComments(src) {
	return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
}

let contract;

before(async () => {
	contract = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-contract.svelte.ts'
	);
});

// REQ: LIBMX-06
test('collectionSearchEmptyMessage distinguishes failure from zero hits', () => {
	const { collectionSearchEmptyMessage } = contract;

	assert.equal(collectionSearchEmptyMessage(null, 0), 'no tracks match the search');
	assert.equal(collectionSearchEmptyMessage(null, 3), null);
	assert.equal(
		collectionSearchEmptyMessage('TypeError: Failed to fetch', 0),
		'search failed: TypeError: Failed to fetch'
	);
	assert.doesNotMatch(
		collectionSearchEmptyMessage('TypeError: Failed to fetch', 0),
		/no tracks match/i
	);
});

test('search_error lifecycle on PaneStore', () => {
	const { createPaneStore } = contract;
	const p = createPaneStore();

	assert.equal(p.search_error, null);

	p.search_error = 'TypeError: Failed to fetch';
	p.setWholeCollection(false);
	assert.equal(p.search_error, null);
	assert.deepEqual(p.search_results, []);
	assert.equal(p.searching, false);

	p.search_error = 'still set';
	p.setWholeCollection(true);
	assert.equal(p.search_error, 'still set');

	const seq = p.beginLoad('pl-1', 'Warmup');
	assert.equal(p.search_error, 'still set');
	p.completeLoad(seq, [], '', false);
});

test('BrowserPanel persists search_error on failed whole-collection search', () => {
	const src = stripComments(readFileSync(browserPanelPath, 'utf8'));

	assert.match(src, /active\.search_error\s*=\s*null/, 'non-empty search start clears search_error');
	assert.match(
		src,
		/active\.search_error\s*=\s*String\(exc\)/,
		'catch path must persist search_error for the current query'
	);
	assert.match(src, /pushToast\(`search failed: \$\{String\(exc\)\}`/, 'catch path still toasts');
	assert.match(
		src,
		/trimmed === ''[\s\S]*active\.search_error\s*=\s*null/,
		'empty-query early return clears search_error'
	);
	assert.match(
		src,
		/wholeCollectionActive[\s\S]*collectionSearchEmptyMessage\(pane\.search_error/,
		'collection emptyMessage uses search_error before zero-hit copy'
	);
	assert.match(
		src,
		/onemptyretry=\{[\s\S]*wholeCollectionActive\s*&&\s*pane\.search_error\s*!==\s*null[\s\S]*_searchWholeCollection\(pane,\s*pane\.search\)/,
		'retry is gated on wholeCollectionActive && search_error and re-runs _searchWholeCollection'
	);
	assert.match(
		src,
		/if\s*\(\s*pane\.loading\s*\|\|\s*pane\.searching\s*\)\s*return\s*null/,
		'empty-state still yields while loading or searching'
	);
});

// REQ: LIBMX-06
test('TrackTable renders a gated Retry search button in the empty state', () => {
	const src = stripComments(readFileSync(trackTablePath, 'utf8'));

	assert.match(
		src,
		/\{#if rows\.length === 0 && emptyMessage !== null\}[\s\S]*\{emptyMessage\}[\s\S]*Retry search[\s\S]*\{\/if\}/,
		'empty state renders the message and a Retry search control'
	);
	assert.match(
		src,
		/<button type="button" class="empty-retry" onclick=\{onemptyretry\}>Retry search<\/button>/,
		'retry is a visible button, not icon-only'
	);
	assert.match(
		src,
		/\{#if onemptyretry !== undefined\}[\s\S]*Retry search[\s\S]*\{\/if\}/,
		'retry button is gated on onemptyretry so zero-hit empty states stay text-only'
	);
});
