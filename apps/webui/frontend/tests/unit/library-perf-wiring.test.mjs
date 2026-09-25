import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// PERF-R5 Q9 + Q10 wiring guards.
//
// The pure pieces (createFilterDebounce, memoryReadout) are covered by
// library-browse-instrumentation.test.mjs and memory-meter-model.test.mjs.
// What those cannot see is whether the components still USE them, and the two
// regressions below are both one-line reverts that no other test would catch:
// a Svelte component cannot be loaded by the esbuild harness, so the source is
// asserted the same way engine-source.mjs asserts the audio engine.
//
// Regression lines:
// - if SearchBox renders the owner's `value` again then the input lags the
//   caret by the whole debounce window and Escape mid-burst clears nothing
// - if BrowserPanel writes pane.search straight from a keystroke again then the
//   full-array filter+sort is back to once per character
// - if PerfMeters drops untrack then its "sampled every 2s" comment is false
//   again: a reactive read in the effect body tears the interval down and
//   rebuilds it on every cache mutation
// - if the memory meter stops going through memoryReadout then the WKWebView
//   heap reading is a fabricated 0 MB again

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

test('SearchBox renders its own draft, not the debounced owner value', () => {
	const src = source('src/lib/components/rb/browser/SearchBox.svelte');
	assert.match(
		src,
		/<input[^>]*\n\s*value=\{draft\}/,
		'the input must render the local echo - rendering the owner value puts the ' +
			'whole debounce window between a keypress and the character appearing'
	);
	assert.match(
		src,
		/draft = e\.currentTarget\.value;\s*\n\s*oninput\(draft\);/,
		'the echo must be set BEFORE the owner is told, so the caret never waits'
	);
	assert.match(
		src,
		/\{#if draft\.trim\(\) !== ''\}/,
		'the clear button keys off the echo; keying off the owner value makes it ' +
			'appear a debounce window late'
	);
	assert.equal(
		(src.match(/draft = ''/g) ?? []).length,
		2,
		'both clear paths (Escape and the X button) must reset the echo - the owner ' +
			'value may still be the pre-burst string, so clearing it moves no prop'
	);
});

test('BrowserPanel routes keystrokes through the filter debounce', () => {
	const src = source('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(
		src,
		/function setSearch\(next: string\): void \{\s*\n\s*const paneIndex = activePane;\s*\n\s*_filterDebounceFor\(paneIndex\)\.push\(next\);/,
		'if setSearch writes pane.search directly again then every keystroke runs ' +
			'filterRows + sortRows over the whole pane array'
	);
	assert.match(
		src,
		/_filterDebounceFor\(activePane\)\.cancel\(\);\s*\n\s*panes\[activePane\]\.setSearch\(next\);/,
		'programmatic writes must cancel a pending burst or they get overwritten by it'
	);
	assert.match(
		src,
		/_lastVisibleComputeMs = performance\.now\(\) - startedAt;/,
		'the filter ring row needs the real recompute time, not a second pass run to measure it'
	);
	for (const call of [
		"recordLibraryLoadTiming('all-tracks'",
		"recordLibraryLoadTiming('playlist'",
		'recordPlaylistTreeReadyMs(',
		'recordPlaylistSwitchFirstRowsMs(',
		'recordCollectionSearchTiming(',
		'recordFilterTiming('
	]) {
		assert.ok(src.includes(call), `the library path lost its ${call} instrumentation`);
	}
	assert.match(
		src,
		/recordPlaylistSwitchFirstRowsMs\([\s\S]*?'playlist'/,
		'playlist switch must record first-rows latency'
	);
	assert.match(
		src,
		/recordPlaylistSwitchFirstRowsMs\([\s\S]*?'all-tracks'/,
		'all-tracks switch must record first-rows latency'
	);
});

test('TrackTable row artwork stays thumbnail-sized', () => {
	const src = source('src/lib/components/rb/browser/TrackTable.svelte');
	assert.match(src, /artworkUrl\(row\.stable_id, 's'\)/);
	assert.doesNotMatch(src, /artworkUrl\(row\.stable_id, 'orig'\)/);
});

test('BrowserPanel loads ingestion coverage after primary browser initialization', () => {
	const src = source('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(src, /import \{ getIngestCoverage, type IngestCoverage \} from '\$lib\/rb\/api-ingest';/);
	assert.match(
		src,
		/await _restoreBootPane\(\);[\s\S]*?finally \{[\s\S]*?playlistsLoading = false;[\s\S]*?\}[\s\S]*?void _loadIngestCoverage\(\);/,
		'ingest coverage must start only after the playlists and initial pane settle, never on boot critical path'
	);
	for (const meaning of ['Library health', 'Vocals completion', 'Stems completion']) {
		assert.ok(src.includes(meaning), `the health detail popover must retain ${meaning}`);
	}
	assert.match(src, /state: missing === 0 \? 'complete' : 'incomplete'/);
	assert.match(src, /state: 'unavailable'/);
	assert.match(src, /state: 'error'/);
});

test('coverage counts only reachable audio and refetches through the library refresh gate', () => {
	const src = source('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(src, /const completed = coverage\.on_disk - missing;/);
	assert.match(src, /\$\{coverage\.unreachable\} broken \$\{coverage\.unreachable === 1 \? 'link' : 'links'\}/);
	assert.match(
		src,
		/async function _refreshLibraryRowsOnce\(\): Promise<void> \{\s*await Promise\.all\(\[_loadIngestCoverage\(\), _loadReconcileSummary\(\), _refreshPlaylists\(\)\]\);/
	);
	assert.doesNotMatch(src, /import \{ api, unwrap \} from '\$lib\/api\/client';/);
	const ingest = source('../server/routes/ingest.py');
	assert.match(ingest, /finally:\s*job\.current_step = None\s*job\.finished_at = time\.time\(\)\s*publish\("library\.changed", \{"kind": "tracks", "ids": \[\]\}\)/);
});

test('BrowserPanel renders reconciled playable counts without delaying initial playlist rendering', () => {
	const src = source('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(src, /getReconcileSummary/);
	assert.match(src, /allTracksNonBrokenCount = summary\.total_tracks - summary\.total_broken/);
	assert.match(src, /broken_count: playlistBrokenCount\(p\)/);
	assert.match(
		src,
		/finally \{\s*playlistsLoading = false;[\s\S]*?void _loadReconcileSummary\(\);[\s\S]*?\}/,
		'reconcile must run from _init finally after boot settles, not on the mount critical path'
	);
	const onMountBlock = src.match(/onMount\(\(\) => \{[\s\S]*?\n\t\}\);/)?.[0] ?? '';
	assert.match(onMountBlock, /void _init\(\);/);
	assert.doesNotMatch(
		onMountBlock,
		/void _loadReconcileSummary\(\);/,
		'onMount must not fire reconcile in parallel with _init'
	);
	assert.match(src, /allTracksCount=\{allTracksNonBrokenCount\}/);

	const tree = source('src/lib/components/rb/browser/PlaylistTree.svelte');
	assert.match(tree, /non-broken tracks, \$\{node\.broken_count\} broken tracks/);
	assert.match(tree, /loading non-broken and broken track counts/);
	assert.match(tree, /non-broken count unavailable:/);
	assert.match(tree, /\$\{node\.track_count - node\.broken_count\} non-broken tracks/);
	assert.match(tree, /title=\{_playlistCountTitle\(node\)\}>\{node\.track_count - node\.broken_count\}/);
});

test('BrowserPanel sources mostly-broken policy from runtime-policy and hydrates at boot', () => {
	const src = source('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(src, /from '\$lib\/rb\/runtime-policy\.svelte'/);
	assert.match(src, /hydrateRuntimePolicy/);
	assert.match(src, /playlistMostlyBroken/);
	assert.doesNotMatch(src, /const HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO/);
	const policy = source('src/lib/rb/runtime-policy.svelte.ts');
	assert.match(
		policy,
		/if \(p\.track_count === 0\) return p\.available_count === 0;/,
		'empty playlists must no longer escape the broken-link filter'
	);
});

test('BrowserPanel computes and passes hiddenBrokenPlaylistCount for the playlist tree', () => {
	const src = source('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(
		src,
		/const hiddenBrokenPlaylistCount = \$derived\(\s*uiPrefs\.hide_broken_links\s*\?\s*playlists\.filter\(\s*\(p\) => !isWithinCreateGrace\(p\.playlist_id\) && playlistMostlyBroken\(p\)\s*\)\.length\s*:\s*0\s*\);/
	);
	assert.match(src, /hiddenBrokenPlaylistCount=\{hiddenBrokenPlaylistCount\}/);
});

test('BrowserPanel keeps a playlist inside its create grace visible while broken links are hidden', () => {
	// r3929355475: with Broken unchecked the '+' flow creates a zero-track
	// playlist that the filter would remove before PlaylistTree can focus its
	// rename, so creation appeared to do nothing.
	const src = source('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(src, /isWithinCreateGrace,/);
	assert.match(
		src,
		/!uiPrefs\.hide_broken_links \|\|\s*isWithinCreateGrace\(p\.playlist_id\) \|\|\s*!playlistMostlyBroken\(p\)/,
		'the tree filter must exempt playlists still inside their create grace'
	);
});

test('BrowserPanel tooltip states the real playlist threshold from runtime policy', () => {
	// r3929355481: the tooltip claimed only playlists with no playable tracks
	// vanish, but the predicate hides anything under the server min-available ratio.
	const src = source('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(src, /formatHideBrokenCheckboxTooltip\(\)/);
	const policy = source('src/lib/rb/runtime-policy.svelte.ts');
	assert.match(policy, /including empty ones/);
});

test('BrowserPanel presents the persisted hide preference as an affirmative Broken checkbox', () => {
	const src = source('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(
		src,
		/aria-label="Show broken links"\s*checked=\{!uiPrefs\.hide_broken_links\}\s*onchange=\{\(e\) => setHideBrokenLinks\(!e\.currentTarget\.checked\)\}/,
		'the checked UI state must keep the persisted hide flag inverted at the component boundary'
	);
	assert.match(src, /<span>Broken<\/span>/);
});

test('the row-select prefetch caches emit sampled timings', () => {
	assert.match(
		source('src/lib/components/rb/wave/anlz-cache.svelte.ts'),
		/recordAnlzPrefetchSampled\(performance\.now\(\) - startedAt, 'ready'\)/,
		'the /anlz warm is timed on the miss path or it is not timed at all'
	);
	assert.match(
		source('src/lib/rb/audio-prefetch-cache.svelte.ts'),
		/recordAudioPrefetchSampled\(performance\.now\(\) - startedAt, bytes\.byteLength\)/,
		'the audio warm must report the bytes it actually fetched'
	);
});

test('the memory meter samples through untrack and perfMeterSampleIntervalMs', () => {
	const src = source('src/lib/components/rb/PerfMeters.svelte');
	assert.match(src, /untrack\(_updateMemory\)/);
	assert.match(
		src,
		/perfMeterSampleIntervalMs/,
		'without untrack the reactive reads inside _updateMemory make the effect ' +
			'tear down and recreate the interval on every cache mutation'
	);
	assert.match(
		src,
		/const HEAP_API_PRESENT = typeof performance !== 'undefined' && hasJsHeapApi\(performance\)/,
		'the Chromium-only API is feature-detected ONCE, not re-probed per sample'
	);
	assert.match(
		src,
		/jsHeapMB: HEAP_API_PRESENT \? readJsHeapMB\(performance\) : null/,
		'null, not 0: an unmeasurable heap must not enter the total as a measured zero'
	);
	assert.match(src, /title=\{memoryHover\}/, 'the house rule: every numeric readout keeps its hover');
});

test('PlaylistTree renders the reserved Missing Tracks folder outside the playlist loop', () => {
	const tree = source('src/lib/components/rb/browser/PlaylistTree.svelte');
	assert.match(tree, /MissingTracksFolder/);
	assert.match(tree, /data-testid="playlist-missing-tracks"/);
	const eachBlock = tree.match(/\{#each nodes as node \(node\.playlist_id\)\}[\s\S]*?\{\/each\}/)?.[0];
	assert.ok(eachBlock, 'playlist {#each} loop must still exist');
	assert.doesNotMatch(eachBlock, /playlist-missing-tracks/);
});

test('BrowserPanel loads Missing Tracks via the reserved kind and hide-broken bypass', () => {
	const src = source('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(src, /fetchMissingTrackRows/);
	assert.match(src, /node\.kind === 'missing_tracks'/);
	assert.match(
		src,
		/hide_broken_links && !isMissingTracksId/,
		'the Missing Tracks pane must ignore Hide broken links or every row vanishes'
	);
});

