/**
 * `browser_select_playlist` answers when the selection LANDS.
 *
 * Found live on the silver preview (Tue 6 Oct 2026): the order switched the
 * tab to All Tracks (`playlist=all` in the URL) and then gave no HTTP answer
 * within 20 s, because the page awaited the whole-library index fill before
 * reporting. Agents read that as a timeout for a success.
 *
 * [if] the pane paints its first rows while the fill is still running [then]
 *   the order answers within a few seconds, [else ⛔] it waits on the fill.
 * [if] the fill ends before any paint signal [then] the order answers then.
 * [if] the order path awaits the fill (the old code) [then ⛔] the mutation
 *   control below times out.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { untilSelectionLands } = await loadTypeScriptModule('src/lib/components/rb/browser/selection-lands.ts');

/** A fill that never ends: the slow whole-library index on a loaded box. */
const neverEnds = () => new Promise(() => {});

function withDeadline(promise, ms) {
	return Promise.race([
		promise.then(() => 'answered'),
		new Promise((resolve) => setTimeout(() => resolve('timed out'), ms))
	]);
}

test('the order answers once the pane paints, while the fill still runs', async () => {
	let unsubscribed = 0;
	const started = Date.now();
	const outcome = await withDeadline(
		untilSelectionLands(neverEnds(), (landed) => {
			setTimeout(landed, 30);
			return () => (unsubscribed += 1);
		}),
		3000
	);
	assert.equal(outcome, 'answered');
	assert.ok(Date.now() - started < 3000, 'within a few seconds');
	assert.equal(unsubscribed, 1, 'the landing watch is released');
});

test('a fill that ends first answers too, and a pane already landed answers at once', async () => {
	assert.equal(await withDeadline(untilSelectionLands(Promise.resolve(), () => () => {}), 1000), 'answered');
	let unsubscribed = 0;
	const synchronous = untilSelectionLands(neverEnds(), (landed) => {
		landed();
		return () => (unsubscribed += 1);
	});
	assert.equal(await withDeadline(synchronous, 1000), 'answered');
	assert.equal(unsubscribed, 1);
});

test('mutation control: waiting on the fill instead of the landing times out', async () => {
	const waitOnFill = (load) => load;
	assert.equal(await withDeadline(waitOnFill(neverEnds()), 300), 'timed out');
});

test('the order path waits on the landing, not on the fill', () => {
	const panel = readFileSync(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url), 'utf8');
	const body = panel.slice(panel.indexOf('async function _selectPlaylistFromCommand'), panel.indexOf('async function _selectPlaylistForOrder'));
	assert.match(body, /await untilSelectionLands\(_loadPane\(pane, node\)/);
	assert.doesNotMatch(body, /await _loadPane\(/, 'awaiting the whole fill is the bug');
	assert.match(body, /if \(!pane\.loading\) landed\(\)/, 'landing is the pane leaving its loading state');
	assert.match(panel, /selectPlaylist: _selectPlaylistForOrder/, 'the agent adapter uses the order path');
});

const { paneLoadState } = await loadTypeScriptModule('src/lib/components/rb/browser/selection-lands.ts');

test('a painted pane that is still filling reads complete:false; a finished fill reads complete', () => {
	assert.deepEqual(paneLoadState({ loading: true, load_progress: null, rows: [] }), { complete: false, rows_loaded: 0 });
	// First rows on screen, the rest of the library still arriving.
	assert.deepEqual(paneLoadState({ loading: false, load_progress: { loaded: 500, total: 9000 }, rows: new Array(500) }), { complete: false, rows_loaded: 500 });
	assert.deepEqual(paneLoadState({ loading: false, load_progress: null, rows: new Array(9000) }), { complete: true, rows_loaded: 9000 });
});

test('the order result and the mirror both carry the pane load state', () => {
	const read = (path) => readFileSync(new URL(`../../src/lib/${path}`, import.meta.url), 'utf8');
	assert.match(read('rb/agent-orders.ts'), /order\.type === 'browser_select_playlist' && after\.browser\.load !== null\) \{\s*return \{ status: 'succeeded', result: \{ \.\.\.after\.browser\.load \} \}/);
	assert.match(read('rb/ui-mirror.ts'), /load: state\.browser\.load/);
	assert.match(read('components/rb/BrowserPanel.svelte'), /load: paneLoadState\(p\)/);
});
