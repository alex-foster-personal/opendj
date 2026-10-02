/**
 * BrowserPanel playlist sidebar readiness.
 *
 * SOURCE-SHAPE ON PURPOSE, and the capability is reported unavailable rather
 * than faked: this harness is node:test with no component mount infra (no
 * jsdom, no @testing-library, no .svelte module loader), so BrowserPanel cannot
 * be rendered and queried here. Same precedent and same reason as
 * jobs-drawer.test.mjs. AGENTS.md forbids manufacturing a passing result from a
 * stub DOM, so what is pinned below is every link in the chain a regression
 * would have to break, measured so that each assertion can actually go red.
 *
 * The first draft of this test compared an offset taken inside the _init()
 * slice against one taken from the whole file, so the ordering assertion was
 * trivially true for any placement of the readiness flag. Every offset here is
 * now measured in one index frame.
 *
 * Regression lines:
 * - if playlistsLoading = false moves after the _restoreBootPane() await then
 *   playlist navigation waits for the first track page again
 * - if the All Tracks boot path awaits _restoreBootPane() before the tree is
 *   released then the tree waits for the WHOLE library walk (#3985: 25 s at
 *   10k tracks). The first test only looks after the metadata assignment, so it
 *   could not see that await, which sat before it.
 * - if BrowserPanel stops passing playlistsLoading to SpotifySourcePanel then
 *   the sidebar can never leave its loading state
 * - if SpotifySourcePanel stops gating its rows behind playlistsLoading then
 *   the flag no longer decides whether the sidebar is usable
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { compile } from 'svelte/compiler';

const panelUrl = new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url);
const sidebarUrl = new URL(
	'../../src/lib/components/rb/browser/SpotifySourcePanel.svelte',
	import.meta.url
);
const panel = readFileSync(panelUrl, 'utf8');
const sidebar = readFileSync(sidebarUrl, 'utf8');

/** One index frame: every offset in the ordering test is measured inside this. */
const initStart = panel.indexOf('async function _init()');
const initEnd = panel.indexOf('async function _restoreBootPane()');
assert.ok(initStart >= 0 && initEnd > initStart, 'could not isolate _init() in BrowserPanel.svelte');
const init = panel.slice(initStart, initEnd);

test('playlist sidebar becomes usable when metadata arrives, before the all-track page walk', () => {
	const metadata = init.indexOf('playlists = reconcileBootSnapshot(');
	assert.ok(metadata >= 0, 'expected _init() to assign the hydrated playlists');
	const tracks = init.indexOf('await _restoreBootPane();', metadata);
	assert.ok(tracks > metadata, 'expected _init() to await the first track pane after the playlists');
	const ready = init.indexOf('playlistsLoading = false;', metadata);
	assert.ok(ready > metadata, 'expected _init() to clear the loading flag after the playlists land');
	assert.ok(ready < tracks, 'playlist navigation must not wait for every track to load');
	compile(panel, { filename: panelUrl.pathname, generate: 'client' });
});

test('the All Tracks boot path does not await the pane before releasing the tree (#3985)', () => {
	const ready = init.indexOf('playlistsLoading = false;');
	assert.ok(ready >= 0, 'expected _init() to clear the loading flag');
	const beforeReady = init.slice(0, ready);
	assert.ok(
		beforeReady.includes('bootPaneDone = _restoreBootPane();'),
		'expected the All Tracks boot path to start the pane before the tree is released'
	);
	assert.equal(
		/await\s+_restoreBootPane\(/.test(beforeReady),
		false,
		'the tree must not wait for _restoreBootPane(): fillAllTracksPane walks every page'
	);
	const settle = init.indexOf('if (bootPaneDone !== null) await bootPaneDone;', ready);
	const caught = init.indexOf('} catch (exc) {', ready);
	assert.ok(
		settle > ready && settle < caught,
		'the started pane must still be awaited inside the try so a failed first page reaches the catch'
	);
});

test('the tree-ready metric is open-to-tree, not now() minus an epoch (#3985)', () => {
	assert.ok(
		init.includes('recordPlaylistTreeReadyMs(Math.round(performance.now()));'),
		'tree-ready must record performance.now(), which counts from navigation start'
	);
	assert.equal(
		init.includes('.startedAt'),
		false,
		'bootTracksPrefetch().startedAt is performance.timeOrigin (an epoch); subtracting it clamps to 0'
	);
});

test('the readiness flag reaches the sidebar and gates whether it renders rows', () => {
	const tagStart = panel.indexOf('<SpotifySourcePanel');
	assert.ok(tagStart >= 0, 'BrowserPanel must render SpotifySourcePanel');
	const tagEnd = panel.indexOf('/>', tagStart);
	assert.ok(tagEnd > tagStart, 'could not find the end of the SpotifySourcePanel tag');
	assert.ok(
		panel.slice(tagStart, tagEnd).includes('playlistsLoading={playlistsLoading}'),
		'BrowserPanel must pass playlistsLoading to SpotifySourcePanel'
	);

	const guard = sidebar.indexOf('{#if playlistsLoading}');
	assert.ok(guard >= 0, 'SpotifySourcePanel must branch on playlistsLoading');
	const rows = sidebar.indexOf('{#each visiblePlaylists as playlist', guard);
	assert.ok(rows > guard, 'the playlist rows must sit in the ready branch after playlistsLoading clears');
	assert.equal(
		sidebar.slice(guard, rows).includes('{/if}'),
		false,
		'the playlist rows must stay inside the playlistsLoading branch, not after it'
	);
	compile(sidebar, { filename: sidebarUrl.pathname, generate: 'client' });
});
