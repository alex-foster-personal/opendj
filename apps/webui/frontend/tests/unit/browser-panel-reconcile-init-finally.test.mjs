/**
 * Issue #3750: reconcile summary must load from _init() finally, not parallel mount.
 *
 * [if] onMount fires reconcile beside _init [then ⛔️] boot tree/pane races it.
 *
 * The failure path (_init() rejects, the count still resolves) is proved by
 * driving the real BrowserPanel in tests/e2e/missing-tracks-folder.spec.ts,
 * "Missing Tracks count still resolves when playlist boot fails (#3750)".
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

test('reconcile wiring lives in _init finally, not parallel onMount', () => {
	const src = panelSource();
	assert.match(
		src,
		/finally \{\s*playlistsLoading = false;[\s\S]*?void _loadReconcileSummary\(\);[\s\S]*?\}/,
		'reconcile must settle in _init finally'
	);
	const onMountBlock = src.match(/onMount\(\(\) => \{[\s\S]*?\n\t\}\);/)?.[0] ?? '';
	assert.match(onMountBlock, /void _init\(\);/);
	assert.doesNotMatch(onMountBlock, /void _loadReconcileSummary\(\);/);
});
