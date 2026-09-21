/**
 * Issue #3750: reconcile summary must load from _init() finally, not parallel mount.
 *
 * [if] _init() throws during playlist boot [then ⛔️] reconcile still resolves.
 * [if] onMount fires reconcile beside _init [then ⛔️] boot tree/pane races it.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

function panelSource() {
	return readFileSync(PANEL, 'utf8');
}

function extractLoadReconcileSummary() {
	const source = panelSource();
	const start = source.indexOf('\tasync function _loadReconcileSummary(): Promise<void> {');
	const end = source.indexOf('\n\tasync function _init():', start);
	assert.ok(start >= 0 && end > start, 'could not isolate BrowserPanel._loadReconcileSummary');
	const body = source
		.slice(start, end)
		.replace(
			'async function _loadReconcileSummary(): Promise<void> {',
			'async function loadReconcileSummary(state, getReconcileSummary) {'
		)
		.replace('catch (error: unknown)', 'catch (error)')
		.replace('allTracksNonBrokenCount =', 'state.allTracksNonBrokenCount =')
		.replace('allTracksBrokenCount =', 'state.allTracksBrokenCount =')
		.replace('allTracksReconcileError =', 'state.allTracksReconcileError =');
	return Function(`${body}\nreturn loadReconcileSummary;`)();
}

test('reconcile wiring lives in _init finally, not parallel onMount', () => {
	const src = panelSource();
	assert.match(
		src,
		/finally \{\s*playlistsLoading = false;[\s\S]*?void _loadReconcileSummary\(\);[\s\S]*?\}[\s\S]*?void _loadIngestCoverage\(\);/,
		'reconcile must settle in _init finally before the success-only ingest tail'
	);
	const onMountBlock = src.match(/onMount\(\(\) => \{[\s\S]*?\n\t\}\);/)?.[0] ?? '';
	assert.match(onMountBlock, /void _init\(\);/);
	assert.doesNotMatch(onMountBlock, /void _loadReconcileSummary\(\);/);
});

test('reconcile resolves when _init throws after playlist boot fails', async () => {
	const state = {
		allTracksBrokenCount: null,
		allTracksNonBrokenCount: null,
		allTracksReconcileError: null
	};
	const loadReconcile = extractLoadReconcileSummary();
	const getReconcileSummary = async () => ({ total_tracks: 10, total_broken: 2 });

	async function simulateInitWithFailedBoot() {
		try {
			throw new Error('playlist boot failed');
		} catch (exc) {
			throw exc;
		} finally {
			await loadReconcile(state, getReconcileSummary);
		}
	}

	await simulateInitWithFailedBoot().catch(() => {});
	assert.equal(state.allTracksBrokenCount, 2);
	assert.equal(state.allTracksNonBrokenCount, 8);
	assert.equal(state.allTracksReconcileError, null);
});
