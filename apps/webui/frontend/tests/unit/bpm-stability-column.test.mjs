// Issue #7879 (TEMPOROW-8874b): the BPM Stability cell, RENDERED with Svelte's
// server renderer. It prints the daemon's text and tone verbatim, appends
// "· grid?" only for a suspect grid, stays blank when the tempo was never
// judged, and the column is hideable as "Beat stability" right after BPM.
// [if] a served tempo block is rendered [then] the markup carries exactly its
// text and tone with the dim arrow, and nothing is computed, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = [
	"export { default as BpmStabilityCell } from '$lib/components/rb/browser/BpmStabilityCell.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;
let cols;
let titles;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
	cols = await loadTypeScriptModule('src/lib/rb/library-columns.ts');
	titles = await loadTypeScriptModule('src/lib/components/rb/browser/bpm-stability-title.ts');
});

const tempo = (over) => ({
	verdict: 'changes',
	largest_change_pct: 18,
	change_count: 3,
	text: '3× change',
	tone: 'deep_red',
	title: 'Tempo changes (largest move 18% from the main tempo, 3 confirmed changes). Beatgrid: ok',
	...over
});
const grid = (over) => ({ grid_class: 'ok', reason: null, dismissed: false, message: null, tempo: tempo({}), ...over });
const html = (g) => mod.render(mod.BpmStabilityCell, { props: { grid: g } }).body;

test('a served block renders its text and tone verbatim with the dim arrow', () => {
	const out = html(grid({}));
	assert.match(out, /class="tempostab tone-deep_red[^"]*"/);
	assert.match(out, /class="tempostab-arrow[^"]*" aria-hidden="true">←<\/span> 3× change/);
	assert.doesNotMatch(out, /grid\?/, 'an ok grid carries no grid? marker');
});

test('the text is whatever the daemon sent, never recomputed from the numbers', () => {
	const out = html(grid({ tempo: tempo({ text: 'drift <1%', tone: 'amber', largest_change_pct: 0.6 }) }));
	assert.match(out, /drift &lt;1%|drift <1%/);
	assert.doesNotMatch(out, /0\.6|±1%/);
});

test('a suspect grid appends "· grid?" with the grid message as its title', () => {
	const out = html(grid({ grid_class: 'suspect', message: 'Beatgrid: 300 uneven intervals' }));
	assert.match(out, /title="Beatgrid: 300 uneven intervals"[^>]*> · grid\?/);
	assert.equal(
		titles.bpmStabilityTitle(grid({ grid_class: 'suspect', message: 'M' })),
		`${tempo({}).title} M`
	);
});

test('control: never judged renders nothing and has no title', () => {
	for (const g of [grid({ tempo: null }), null, undefined]) {
		assert.doesNotMatch(html(g), /tempostab|steady|change/);
		assert.equal(titles.bpmStabilityTitle(g), undefined);
	}
});

test('the column sits right after BPM and is hideable as "Beat stability"', () => {
	const order = [...cols.HIDEABLE_COLUMNS];
	assert.equal(order[order.indexOf('bpm') + 1], 'tempostab');
	assert.equal(cols.HIDEABLE_COLUMN_LABELS.tempostab, 'Beat stability');
});
