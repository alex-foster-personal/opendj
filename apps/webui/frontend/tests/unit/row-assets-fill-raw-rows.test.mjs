// LIBM-13678: a row-assets answer (vocals, cover verdict, stems, tempo curve)
// lands on raw pane rows (PR #12724) by publishing a whole-row replacement, so
// anything reading the row re-runs. Written in place it never redrew: the cover,
// stems and vocals marks stayed blank until something else re-rendered. Runs the
// REAL Svelte runtime via load-rune-module.mjs. -Claude
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = [
	"import { flushSync } from 'svelte';",
	"import { createPaneStore, updatePaneRow } from '$lib/components/rb/browser/pane-contract.svelte';",
	"import { applyRowAssetsAnswer } from '$lib/rb/row-assets-fill';",
	'function _row(id: string) {',
	"	return { stable_id: id, order: 1, vocals: { status: 'not_analyzed' }, artwork_available: null, artwork_status: 'unresolved', stems: null } as never;",
	'}',
	'export function fill(pending: string[]) {',
	'	const pane = createPaneStore();',
	"	pane.rows = [_row('t1'), _row('t2')];",
	'	const untouched = pane.rows[1];',
	'	const seen: string[] = [];',
	'	const stop = $effect.root(() => {',
	'		$effect(() => { seen.push(`${(pane.rows[0] as any).vocals.status}/${(pane.rows[0] as any).artwork_available}`); });',
	'	});',
	'	flushSync();',
	'	const settled = new WeakSet<object>();',
	'	const answer = {',
	"		assets: { t1: { preview_b64: null, preview_max: null, vocals: { status: 'no_vocals', fps: 10, regions: [] }, artwork_available: true, artwork_status: 'ok', stems: { status: 'invalid', error: 'bad bundle' }, tempo: null } },",
	'		pending',
	'	};',
	"	const strips = applyRowAssetsAnswer(['t1'], answer as never, () => pane.rows as never, settled, (id, map) => updatePaneRow(pane, id, map as never));",
	'	flushSync();',
	'	stop();',
	'	return { seen, strips, settledNew: settled.has(pane.rows[0]), untouched: pane.rows[1] === untouched };',
	'}'
].join('\n');

test('[if] a row-assets answer lands on a raw pane row [then] an effect reading that row re-runs with the cover and vocals [else stop]', async () => {
	const live = await loadRuneModule(ENTRY);
	const out = live.fill([]);
	assert.deepEqual(out.seen, ['not_analyzed/null', 'no_vocals/true']);
	assert.equal(out.settledNew, true, 'the published row object is the settled one');
	assert.equal(out.untouched, true, 'a row not asked for keeps its object');
	assert.deepEqual(out.strips, { strips: { t1: null }, pending: [] });
});

test('[if] the answer still lists the row as pending [then] the row redraws but is not settled [else stop]', async () => {
	const live = await loadRuneModule(ENTRY);
	const out = live.fill(['t1']);
	assert.equal(out.seen.at(-1), 'no_vocals/true');
	assert.equal(out.settledNew, false);
});
