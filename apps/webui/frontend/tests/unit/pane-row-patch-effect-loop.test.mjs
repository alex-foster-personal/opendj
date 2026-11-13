// LIBM-13678: a row patch made from inside an $effect must not subscribe that
// effect to the rows array it replaces. BrowserPanel's selected-row strip effect
// called applyDecodedStripAcrossPanes on every run; once PaneStore.rows became a
// $state.raw array replaced whole (PR #12724), each write re-ran the effect until
// Svelte threw effect_update_depth_exceeded, and the aborted flushes left the first
// row's deck buttons unclickable under the header. Runs the REAL Svelte runtime
// via load-rune-module.mjs. -Claude
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = [
	"import { flushSync } from 'svelte';",
	"import { applyDecodedStripAcrossPanes, createPaneStore, updatePaneRow } from '$lib/components/rb/browser/pane-contract.svelte';",
	'export function stripEffectRuns(): number {',
	'	const pane = createPaneStore();',
	"	pane.rows = [{ stable_id: 'sid-a', order: 1, strip: null, revealed: false } as never];",
	'	let runs = 0;',
	'	const stop = $effect.root(() => {',
	'		$effect(() => {',
	'			runs += 1;',
	"			applyDecodedStripAcrossPanes([pane], 'sid-a', { columns: 'e30=', max: runs } as never);",
	'		});',
	'	});',
	'	try { flushSync(); } finally { stop(); }',
	'	return runs;',
	'}',
	'export function noOpPatchKeepsArray(): boolean {',
	'	const pane = createPaneStore();',
	"	pane.rows = [{ stable_id: 'sid-a', order: 1, revealed: true } as never];",
	'	const before = pane.rows;',
	"	updatePaneRow(pane, 'sid-a', (row) => row);",
	'	return pane.rows === before;',
	'}'
].join('\n');

test('[if] an $effect publishes a decoded strip into a raw pane row [then] it runs once, not until effect_update_depth_exceeded [else stop]', async () => {
	const live = await loadRuneModule(ENTRY);
	assert.equal(live.stripEffectRuns(), 1);
});

test('[if] a row patch returns the row it was given [then] the rows array is not republished [else stop]', async () => {
	const live = await loadRuneModule(ENTRY);
	assert.equal(live.noOpPatchKeepsArray(), true);
});
