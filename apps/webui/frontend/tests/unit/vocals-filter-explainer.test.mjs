import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// REQ: LIBHY-12654
// VOCALS-FILTER-01: synthesized dependency rows exercise the real filter and
// BrowserPanel's actual message selector. Source checks cover Svelte wiring,
// not visible browser acceptance.
const panel = readFileSync(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url),
	'utf8'
);
const table = readFileSync(
	new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url),
	'utf8'
);
let contract;
let dependencies;

before(async () => {
	contract = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	const missing = await loadTypeScriptModule('src/lib/components/rb/browser/missing-tracks.ts');
	const autolist = await loadTypeScriptModule('src/lib/components/rb/browser/autolist-ids.ts');
	const selection = await loadTypeScriptModule('src/lib/smartlists/autolist-rule.ts');
	dependencies = { ...contract, ...missing, ...autolist, ...selection };
});

const prefs = {
	vocals_filter: true,
	remixes_filter: false,
	available_offline_filter: false,
	library_key_filter: [],
	next_only_filter: false
};
const row = (stable_id, vocals = { status: 'not_analyzed' }) => ({
	stable_id,
	vocals,
	lyrics: null,
	file_exists: true,
	title: stable_id,
	artist: '',
	album: ''
});
const region = { start_s: 1, end_s: 9, intensity: 3 };
const unknowns = [row('unknown-1'), row('unknown-2'), row('unknown-3')];
const rows = [
	row('rekordbox', { status: 'rekordbox', regions: [region] }),
	row('demucs', { status: 'demucs', regions: [region] }),
	row('negative', { status: 'no_vocals', regions: [] }),
	...unknowns
];

test('region-positive rows without lyrics pass; unknowns never inflate matching counts', () => {
	const selected = contract.applyLibraryFilters(rows, prefs);
	assert.deepEqual(
		selected.map((r) => r.stable_id),
		['rekordbox', 'demucs']
	);
	const counts = contract.vocalsFilterCounts(rows);
	assert.deepEqual(counts, { matching: 2, without: 1, unknown: 3 });
	assert.equal(counts.matching, selected.length);
	const title = contract.vocalsFilterTitle(counts);
	for (const text of [
		'vocal regions',
		'"vocal" lyric verdict',
		'at least 6 lines',
		'2 with vocals, 1 without, 3 not analyzed',
		'excluded until vocals or lyrics are known'
	]) {
		assert.ok(title.includes(text), text);
	}
	assert.deepEqual(contract.vocalsFilterCounts(unknowns), { matching: 0, without: 0, unknown: 3 });
	assert.match(
		contract.vocalsFilterTitle(contract.vocalsFilterCounts(unknowns)),
		/0 with vocals.*3 not analyzed/
	);
});

test('checkbox binds the shared explanation to counts from active rows before filtering', () => {
	assert.match(
		panel,
		/vocalsFilterCounts,[\s\S]*vocalsFilterTitle[\s\S]*from '\.\/browser\/pane-contract\.svelte'/
	);
	assert.match(
		panel,
		/const vocalsSourceRows = \$derived\(\s*wholeCollectionActive \? pane\.search_results : pane\.rows\s*\)/
	);
	assert.match(panel, /const vocalsCounts = \$derived\(vocalsFilterCounts\(vocalsSourceRows\)\)/);
	assert.match(panel, /const vocalsTitle = \$derived\(vocalsFilterTitle\(vocalsCounts\)\)/);
	assert.match(panel, /class="vocals-filter"\s+title=\{vocalsTitle\}/);
	assert.match(
		panel,
		/checked=\{uiPrefs\.vocals_filter\}[\s\S]*onchange=\{\(e\) => setVocalsFilter\(e\.currentTarget\.checked\)\}/
	);
	assert.doesNotMatch(panel, /real word-level lyrics spanning|no tracks here with >5 lines/);
});

// Run the unchanged selector body from the component, with its real helper
// dependencies. This cannot stand in for a mounted UI or a real listing.
function emptyMessage(overrides = {}) {
	const body = panel.match(
		/const emptyMessage = \$derived\.by\(\(\): string \| null => \{([\s\S]*?)\n\t\}\);/
	);
	assert.ok(body, 'BrowserPanel owns the empty-state selector');
	const context = {
		searchFilterFallback: null,
		searchMode: 'filter',
		wholeCollectionActive: false,
		pane: {
			rows: unknowns,
			search_results: [],
			loading: false,
			searching: false,
			error: null,
			search_error: null,
			search: '',
			playlist_id: 'playlist'
		},
		visibleRows: [],
		hideBrokenForActivePane: true,
		uiPrefs: prefs,
		autolistSelection: dependencies.emptyAutolistSelection(),
		nextOnlyRef: null,
		...overrides
	};
	context.vocalsSourceRows = context.wholeCollectionActive
		? context.pane.search_results
		: context.pane.rows;
	context.vocalsCounts = contract.vocalsFilterCounts(context.vocalsSourceRows);
	const names = [
		'filterRows',
		'isMissingTracksId',
		'isAutolistId',
		'hasAutolistSelection',
		'collectionSearchEmptyMessage'
	];
	const values = Object.fromEntries(names.map((name) => [name, dependencies[name]]));
	const scope = { ...values, ...context };
	return new Function(...Object.keys(scope), body[1])(...Object.values(scope));
}

function assertVocalsExplanation(message) {
	assert.match(message, /No tracks with known vocals/i);
	assert.match(message, /vocal regions.*vocal lyric verdict.*at least 6 lines/i);
	assert.match(message, /unknown vocal status.*excluded/i);
}

test('unknown-only list explains vocals with the default Broken preference, including Find', () => {
	assertVocalsExplanation(emptyMessage());
	assertVocalsExplanation(emptyMessage({ searchMode: 'find' }));
	assertVocalsExplanation(emptyMessage({ hideBrokenForActivePane: false }));
});

test('whole-collection exclusions explain vocals while zero hits and errors retain meaning', () => {
	const pane = {
		rows: [],
		search_results: unknowns,
		search: 'unknown',
		playlist_id: 'all',
		loading: false,
		searching: false,
		error: null,
		search_error: null
	};
	assertVocalsExplanation(emptyMessage({ wholeCollectionActive: true, pane }));
	assert.equal(
		emptyMessage({ wholeCollectionActive: true, pane: { ...pane, search_results: [] } }),
		'no tracks match the search'
	);
	assert.equal(
		emptyMessage({ wholeCollectionActive: true, pane: { ...pane, search_error: 'offline' } }),
		'search failed: offline'
	);
});

test('loading, real load failures, empty sources and all-broken lists keep their messages', () => {
	const pane = {
		rows: unknowns,
		search_results: [],
		search: '',
		playlist_id: 'playlist',
		loading: false,
		searching: false,
		error: null,
		search_error: null
	};
	for (const flag of ['loading', 'searching']) {
		assert.equal(emptyMessage({ pane: { ...pane, [flag]: true } }), null);
	}
	assert.equal(emptyMessage({ pane: { ...pane, error: 'offline' } }), 'load failed: offline');
	assert.equal(
		emptyMessage({ searchMode: 'find', pane: { ...pane, error: 'offline' } }),
		'load failed: offline'
	);
	assert.equal(emptyMessage({ pane: { ...pane, rows: [] } }), 'empty playlist');
	assert.equal(
		emptyMessage({ pane: { ...pane, rows: unknowns.map((r) => ({ ...r, file_exists: false })) } }),
		'all tracks in this list are broken links (hidden by Broken filter)'
	);
	assert.equal(emptyMessage({ uiPrefs: { ...prefs, vocals_filter: false } }), 'empty playlist');
	assert.equal(emptyMessage({ visibleRows: [rows[0]] }), null);
	assert.equal(
		emptyMessage({
			pane: { ...pane, rows: [rows[0]] },
			uiPrefs: { ...prefs, remixes_filter: true }
		}),
		'no remixes in this list (title markers: remix/bootleg/rework/VIP/edit)'
	);
});

test('the table renders the selected explanation only in its empty state and preserves retry', () => {
	assert.match(panel, /<TrackTable[\s\S]*\{emptyMessage\}/);
	assert.match(
		table,
		/\{#if rows\.length === 0 && emptyMessage !== null\}[\s\S]*<span data-testid="track-table-empty-message">\{emptyMessage\}<\/span>/
	);
	assert.match(
		table,
		/\{#if onemptyretry !== undefined\}[\s\S]*onclick=\{onemptyretry\}>Retry search<\/button>/
	);
});
