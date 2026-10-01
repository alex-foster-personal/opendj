/**
 * @pytest.mark.requirement GRIDFLAG-05
 * Pin f85c5881b68e: the BPM hover said nothing about the track's beatgrid.
 *
 * [if] the row carries an ok verdict [then] the BPM hover says the grid is
 *   evenly spaced and that this is a spacing check only
 * [if] the row carries an uneven or variable-tempo verdict [then] the BPM
 *   hover carries the same sentence as the Err column
 * [if] the row carries no verdict [then] the BPM hover says the grid was not
 *   checked, never that it is fine
 * [if] any verdict is shown [then] the hover says which analysis made the
 *   grid and its confidence are not available in the list
 *
 * Regression lines:
 * - if a row with no verdict reads as evenly spaced then unknown became ok
 * - if the BPM cell stops calling bpmGridHoverText then the hover is bare again
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let issues;
before(async () => {
	issues = await loadTypeScriptModule('src/lib/rb/analysis-issues.ts');
});

const TABLE = readFileSync(
	fileURLToPath(
		new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
	),
	'utf8'
);
const verdict = (grid_class, message = null, dismissed = false, reason = null) => ({
	grid_quality: { grid_class, message, dismissed, reason }
});

test('an ok grid reads as evenly spaced, spacing only', () => {
	const text = issues.bpmGridHoverText(verdict('ok'));
	assert.match(text, /evenly spaced/);
	assert.match(text, /spacing only/);
});

test('a flagged grid carries the Err column sentence', () => {
	const row = verdict('suspect', 'Beatgrid: uneven spacing, 12 of 400 beats off');
	const text = issues.bpmGridHoverText(row);
	assert.ok(text.includes(issues.errColumnTitle(row)));
	const variable = issues.bpmGridHoverText(verdict('variable_tempo', 'Beatgrid: variable tempo'));
	assert.match(variable, /variable tempo/);
});

test('an unjudged grid carries its reason sentence, never ok', () => {
	const text = issues.bpmGridHoverText(verdict('unknown', 'Beatgrid: not judged (no grid)'));
	assert.match(text, /not judged/);
	assert.doesNotMatch(text, /evenly spaced/);
});

test('a row with no verdict says the grid was not checked', () => {
	for (const row of [{}, { grid_quality: null }]) {
		const text = issues.bpmGridHoverText(row);
		assert.match(text, /Beatgrid: not checked/);
		assert.doesNotMatch(text, /evenly spaced/);
	}
});

test('every variant says source and confidence are not in the list', () => {
	for (const row of [{}, verdict('ok'), verdict('suspect', 'Beatgrid: uneven')]) {
		assert.match(issues.bpmGridHoverText(row), /not implemented - see PARITY-TODO/);
		assert.match(issues.bpmGridHoverText(row), /confidence/);
	}
});

test('the BPM cell hover includes the grid text', () => {
	const fn = TABLE.slice(TABLE.indexOf('function bpmCellTitle'), TABLE.indexOf('function _nowRatioFor'));
	assert.match(fn, /bpmGridHoverText\(row\)/);
});
