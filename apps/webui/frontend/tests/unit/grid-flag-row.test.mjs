/**
 * The library row's beatgrid flag (GRIDFLAG-03, GRIDFLAG-04): what the Err
 * column lights, says, and sorts by, from the verdict the server stored on
 * the row. Pure logic in `src/lib/rb/analysis-issues.ts`.
 *
 * Regression lines:
 * - if a `suspect` row lights no dot, or lights it without its sentence, then broken
 * - if `variable_tempo` is drawn with the same severity as `suspect` then the
 *   two classes are indistinguishable: broken
 * - if `unknown` is drawn as ok (nothing) or as a flag then broken
 * - if a dismissed flag still lights then the override does nothing: broken
 * - if a dismissed flag vanishes entirely then it cannot be restored: broken
 * - if `ok` lights anything then every healthy row is noise: broken
 * - if sorting by the flag does not put flagged rows first then the filter
 *   the column offers does not work
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let issues;
let jobs;

const SUSPECT_MESSAGE =
	'Beatgrid: 223 of 616 beat intervals are uneven (worst 30 ms off at 137.5 s), but 96% of beats sit on ' +
	'one 128.00 BPM line (the grid is uneven, the music is not) - Beat Sync may wander on this track';
const VARIABLE_MESSAGE =
	'Beatgrid: tempo changes through this track: 400 of 500 beat intervals are uneven (worst 80 ms off at ' +
	'12.0 s), 120 tempo markers - Beat Sync may wander on this track';
const UNKNOWN_MESSAGE = 'Beatgrid not checked: the rekordbox analysis file is missing';

const row = (grid_quality, extra = {}) => ({ grid_quality, ...extra });
const SUSPECT = { grid_class: 'suspect', reason: null, dismissed: false, message: SUSPECT_MESSAGE };
const VARIABLE = { grid_class: 'variable_tempo', reason: null, dismissed: false, message: VARIABLE_MESSAGE };
const UNKNOWN = { grid_class: 'unknown', reason: 'anlz_missing', dismissed: false, message: UNKNOWN_MESSAGE };
const OK = { grid_class: 'ok', reason: null, dismissed: false, message: null };
const LEGACY_ISSUE = { severity: 'error', at_sec: 132.4, field_bpm: 128, interval_bpm: 124.7, disagreement_bpm: 3.3 };

before(async () => {
	issues = await loadTypeScriptModule('src/lib/rb/analysis-issues.ts');
	jobs = await loadTypeScriptModule('src/lib/rb/job-progress.svelte.ts');
});

test('a suspect grid lights the beatgrid slot as a warning, carrying the measured sentence', () => {
	const map = issues.analysisIssuesFor(row(SUSPECT));
	assert.deepEqual(Object.keys(map), ['beatgrid']);
	assert.equal(map.beatgrid.severity, 'warning');
	assert.equal(map.beatgrid.detail, SUSPECT_MESSAGE);
	assert.match(map.beatgrid.detail, /223 of 616/, 'the number is in the hover text');
});

test('variable tempo lights a different, quieter severity than an uneven grid', () => {
	const map = issues.analysisIssuesFor(row(VARIABLE));
	assert.equal(map.beatgrid.severity, 'info');
	assert.equal(map.beatgrid.detail, VARIABLE_MESSAGE);
	assert.notEqual(jobs.ANALYSIS_ISSUE_COLORS.info, jobs.ANALYSIS_ISSUE_COLORS.warning);
});

test('unknown is its own state: not ok, not a flag', () => {
	const map = issues.analysisIssuesFor(row(UNKNOWN));
	assert.equal(map.beatgrid.severity, 'unknown');
	assert.equal(map.beatgrid.detail, UNKNOWN_MESSAGE);
	assert.equal(issues.gridFlagFor(row(UNKNOWN)), null, 'nothing to dismiss on an unknown grid');
	assert.equal(issues.flaggedIssueCount(map), 0, 'unknown is not counted as a detected issue');
	assert.equal(issues.flaggedIssueCount(issues.analysisIssuesFor(row(SUSPECT))), 1);
	// CONTROL: ok and unknown must not render the same.
	assert.notDeepEqual(issues.analysisIssuesFor(row(OK)), map);
});

test('an ok grid lights nothing, and the stored verdict outranks the old field detector', () => {
	assert.deepEqual(issues.analysisIssuesFor(row(OK)), {});
	assert.deepEqual(issues.analysisIssuesFor(row(OK, { rb_meta: { beatgrid_issue: LEGACY_ISSUE } })), {});
	// With no judged grid, the older detector is still the only evidence there is.
	const fallback = issues.analysisIssuesFor(row(UNKNOWN, { rb_meta: { beatgrid_issue: LEGACY_ISSUE } }));
	assert.equal(fallback.beatgrid.severity, 'error');
	assert.match(fallback.beatgrid.detail, /3\.3 BPM/);
	// A row that carries no verdict at all (an older payload) is unchanged.
	assert.deepEqual(issues.analysisIssuesFor({}), {});
	assert.equal(issues.analysisIssuesFor({ rb_meta: { beatgrid_issue: LEGACY_ISSUE } }).beatgrid.severity, 'error');
});

test('a dismissed flag lights nothing but stays restorable', () => {
	for (const verdict of [SUSPECT, VARIABLE]) {
		const dismissed = row({ ...verdict, dismissed: true });
		assert.deepEqual(issues.analysisIssuesFor(dismissed), {}, 'the override hides the dot');
		const flag = issues.gridFlagFor(dismissed);
		assert.equal(flag.dismissed, true);
		assert.equal(flag.message, verdict.message, 'the sentence is still there to read');
		assert.match(issues.errColumnTitle(dismissed), /^Beatgrid flag dismissed - /);
	}
	const live = issues.gridFlagFor(row(SUSPECT));
	assert.deepEqual(live, { gridClass: 'suspect', dismissed: false, message: SUSPECT_MESSAGE });
	assert.equal(issues.gridFlagFor(row(OK)), null);
	assert.equal(issues.gridFlagFor({}), null);
});

test('the cell hover title says what was measured, or defers to the default', () => {
	assert.equal(issues.errColumnTitle(row(SUSPECT)), SUSPECT_MESSAGE);
	assert.equal(issues.errColumnTitle(row(VARIABLE)), VARIABLE_MESSAGE);
	assert.equal(issues.errColumnTitle(row(UNKNOWN)), UNKNOWN_MESSAGE);
	assert.match(issues.errColumnTitle(row(OK)), /^Beatgrid: evenly spaced/);
	assert.equal(issues.errColumnTitle({}), undefined, 'no verdict on the row: the default title stands');
});

test('sorting by the flag puts uneven grids first, then variable tempo, then dismissed', () => {
	const rows = [
		row(OK),
		row(UNKNOWN),
		row({ ...SUSPECT, dismissed: true }),
		row(VARIABLE),
		row(SUSPECT)
	];
	const ranked = rows.map((item) => issues.gridFlagSortValue(item));
	assert.deepEqual(ranked, [4, 3, 2, 1, 0]);
	assert.equal(issues.gridFlagSortValue({}), null, 'no verdict sorts last, like every other missing value');
});

test('every severity the mapping can produce has a color', () => {
	for (const severity of ['warning', 'error', 'info', 'unknown']) {
		assert.equal(typeof jobs.ANALYSIS_ISSUE_COLORS[severity], 'string', severity);
	}
});

test('SOURCE: the Err cell, the sort key and the dismiss control are wired', () => {
	const read = (relative) => readFileSync(fileURLToPath(new URL(`../../src/lib/${relative}`, import.meta.url)), 'utf8');
	const table = read('components/rb/browser/TrackTable.svelte');
	assert.match(table, /gridFlag=\{gridFlagFor\(row\)\}/);
	assert.match(table, /title=\{errColumnTitle\(row\)\}/);
	assert.match(table, /ongridflagdismiss=\{\(dismissed\) => _setGridFlagDismissed\(row, dismissed\)\}/);
	assert.match(table, /onsort\('grid'\)/, 'the Err header sorts flagged tracks to the top');
	const contract = read('components/rb/browser/pane-contract.svelte.ts');
	assert.match(contract, /key === 'grid'\) return gridFlagSortValue\(row\)/);
	const popover = read('components/rb/browser/AnalysisDotsPopover.svelte');
	assert.match(popover, /data-testid="grid-flag-dismiss"/);
	assert.match(popover, /Beatgrid flag not \$\{dismissed \? 'dismissed' : 'restored'\}/, 'a refused dismiss is toasted');
	assert.match(popover, /not implemented - see PARITY-TODO/, 'no handler means inert with a tooltip');
	const dots = read('components/rb/browser/AnalysisDots.svelte');
	assert.match(dots, /class:unknown=/, 'an unknown grid is drawn differently from ok');
	const wire = read('components/rb/browser/browser-row-wire.ts');
	assert.equal((wire.match(/grid_quality: wire\.grid_quality \?\? null|grid_quality: track\.grid_quality \?\? null/g) ?? []).length, 2);
});
