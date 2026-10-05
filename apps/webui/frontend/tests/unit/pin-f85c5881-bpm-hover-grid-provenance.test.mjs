/**
 * @pytest.mark.requirement GRIDFLAG-06
 * Pin f85c5881: the BPM hover names which analysis made the beatgrid and how
 * confident it was, fetched lazily when the cell is hovered.
 *
 * [if] the grid is our own analysis [then] the hover names the backend and the
 *   stored confidence, and says what the number is
 * [if] the grid is rekordbox's [then] the hover says so and that rekordbox
 *   publishes no confidence, never a borrowed figure
 * [if] the own lane is missing or failed [then] the hover gives the reason and
 *   no number
 * [if] the read failed [then] the hover says so with the message, never blank
 * [if] the BPM cell stops requesting the provenance on hover [then] broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let issues;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/grid-provenance.ts');
	issues = await loadTypeScriptModule('src/lib/rb/analysis-issues.ts');
});

const read = (rel) =>
	readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');

const own = (over = {}) => ({
	stable_id: 'a'.repeat(40),
	source: 'own',
	basis: 'selection',
	status: 'ok',
	reason: null,
	backend: 'own_beatgrid',
	backend_version: '1.4.0',
	bpm: 124,
	bpm_confidence: 0.9312,
	...over
});

test('own analysis names the backend and the stored confidence, with its meaning', () => {
	const text = mod.gridProvenanceHoverText({ state: 'ready', value: own() });
	assert.match(text, /Grid source: own analysis/);
	assert.match(text, /own_beatgrid 1\.4\.0/);
	assert.match(text, /Confidence 0\.93/);
	assert.match(text, /0 to 1/, 'the number must say what scale it is on');
	assert.doesNotMatch(text, /rekordbox does not publish/);
});

test('an unmapped track says why it is on own analysis', () => {
	const text = mod.gridProvenanceHoverText({
		state: 'ready',
		value: own({ basis: 'unmapped-default' })
	});
	assert.match(text, /no rekordbox analysis/);
});

test('rekordbox says so and never shows a confidence figure', () => {
	const text = mod.gridProvenanceHoverText({
		state: 'ready',
		value: own({ source: 'rekordbox', status: null, backend: null, backend_version: null, bpm: null, bpm_confidence: null })
	});
	assert.match(text, /Grid source: rekordbox/);
	assert.match(text, /rekordbox does not publish/);
	assert.doesNotMatch(text, /\d\.\d\d/);
});

test('a missing or failed own lane gives the reason and no number', () => {
	for (const status of ['missing', 'failed']) {
		const text = mod.gridProvenanceHoverText({
			state: 'ready',
			value: own({ status, reason: 'decode refused the file', bpm: null, bpm_confidence: null })
		});
		assert.match(text, new RegExp(`own analysis, ${status}`));
		assert.match(text, /decode refused the file/);
		assert.doesNotMatch(text, /Confidence \d/);
	}
});

test('an ok own lane with no confidence is refused, not shown as zero', () => {
	assert.throws(
		() => mod.gridProvenanceHoverText({ state: 'ready', value: own({ bpm_confidence: null }) }),
		/bpm_confidence/
	);
});

test('idle, loading and error are each worded, never blank', () => {
	assert.match(mod.gridProvenanceHoverText({ state: 'idle' }), /hover/i);
	assert.match(mod.gridProvenanceHoverText({ state: 'loading' }), /loading/);
	const failed = mod.gridProvenanceHoverText({ state: 'error', message: 'HTTP 500' });
	assert.match(failed, /could not be read/);
	assert.match(failed, /HTTP 500/);
});

test('a result is reused while fresh and refetched once stale', () => {
	assert.equal(mod.gridProvenanceNeedsFetch(undefined, 1000), true);
	assert.equal(mod.gridProvenanceNeedsFetch({ state: 'loading', at: 1000 }, 999_999), false);
	const fresh = { state: 'ready', value: own(), at: 1000 };
	assert.equal(mod.gridProvenanceNeedsFetch(fresh, 1000 + mod.GRID_PROVENANCE_TTL_MS - 1), false);
	assert.equal(mod.gridProvenanceNeedsFetch(fresh, 1000 + mod.GRID_PROVENANCE_TTL_MS), true);
	assert.equal(mod.gridProvenanceNeedsFetch({ state: 'error', message: 'x', at: 1000 }, 1001), true);
});

test('the BPM hover carries the provenance sentence after the verdict', () => {
	const row = { grid_quality: { grid_class: 'ok', message: null, dismissed: false, reason: null } };
	const text = issues.bpmGridHoverText(row, { state: 'ready', value: own() });
	assert.match(text, /evenly spaced/);
	assert.match(text, /Grid source: own analysis/);
	assert.doesNotMatch(text, /not implemented - see PARITY-TODO/);
});

test('the BPM cell requests the provenance on hover and reads it into the title', () => {
	const table = read('src/lib/components/rb/browser/TrackTable.svelte');
	const fn = table.slice(table.indexOf('function bpmCellTitle'), table.indexOf('function _nowRatioFor'));
	assert.match(fn, /bpmGridHoverText\(row, gridProvenanceFor\(row\.stable_id\)\)/);
	const cell = table.slice(table.indexOf('class="c-bpm"') - 400, table.indexOf('class="c-bpm"') + 900);
	assert.match(cell, /onpointerenter=\{\(\) => requestGridProvenance\(row\.stable_id\)\}/);
});

test('the store fetches the grid-provenance endpoint and keeps a failure visible', () => {
	const store = read('src/lib/rb/grid-provenance.svelte.ts');
	assert.match(store, /\/api\/v1\/tracks\/\{stable_id\}\/grid-provenance/);
	assert.match(store, /state: 'error'/);
	assert.match(store, /gridProvenanceNeedsFetch\(/);
});
