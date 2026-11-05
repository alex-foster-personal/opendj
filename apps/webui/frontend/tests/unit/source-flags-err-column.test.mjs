import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// SRCFLAG-01..03: flag-only source findings reach the Err column and the
// cell hovers, never a status word in a cell.
//
// Regression lines:
// - if a row with no source_flags lights a dot or rings the grid then a finding was invented
// - if a stems_other_recording flag does not light the Stems slot then SRCFLAG-02 is invisible
// - if a length or hard-link flag lights an analysis slot then a file finding is passed off as an analysis
// - if the Err hover drops a source sentence then the DJ cannot see why the grid is ringed

let issues;

before(async () => {
	issues = await loadTypeScriptModule('src/lib/rb/analysis-issues.ts');
});

const LENGTH = {
	code: 'length_mismatch',
	severity: 'warning',
	message: 'Maybe a different recording: this file is 275.8 s, the release is 287.6 s (-4.1 %, Spotify spotify:track:7bGRsb8OsRn8eIB7dhEaQN)'
};
const STEMS = {
	code: 'stems_other_recording',
	severity: 'warning',
	message: 'Stems were made from a different recording: their source was 475.0 s, this file is 472.6 s'
};
const LINK = {
	code: 'hardlinked_write',
	severity: 'info',
	message: 'Written in place on Wed 04 Nov 21:33 UTC through a hard link: the same write changed 1 other file name (tags or audio)'
};

test('a row without source flags lights nothing and rings nothing', () => {
	assert.deepEqual(issues.analysisIssuesFor({}), {});
	assert.deepEqual(issues.analysisIssuesFor({ source_flags: [] }), {});
	assert.deepEqual(issues.sourceFileFlagsFor({}), []);
	assert.equal(issues.lengthFlagText({ source_flags: [] }), null);
	assert.equal(issues.errColumnTitle({}), undefined);
});

test('a stem-set from another recording lights only the Stems slot', () => {
	const map = issues.analysisIssuesFor({ source_flags: [STEMS] });
	assert.deepEqual(Object.keys(map), ['stems']);
	assert.equal(map.stems.severity, 'warning');
	assert.equal(map.stems.detail, STEMS.message);
	assert.deepEqual(issues.sourceFileFlagsFor({ source_flags: [STEMS] }), []);
});

test('length and hard-link findings ring the grid, never an analysis slot', () => {
	const row = { source_flags: [LENGTH, LINK] };
	assert.deepEqual(issues.analysisIssuesFor(row), {});
	assert.deepEqual(issues.sourceFileFlagsFor(row), [LENGTH, LINK]);
});

test('the Err hover carries every source sentence ahead of the beatgrid verdict', () => {
	const row = {
		source_flags: [LENGTH, LINK],
		grid_quality: { grid_class: 'ok', reason: null, dismissed: false, message: null }
	};
	const title = issues.errColumnTitle(row);
	assert.ok(title.startsWith(LENGTH.message));
	assert.ok(title.includes(LINK.message));
	assert.ok(title.endsWith('(spacing only, not checked against the audio)'));
});

test('the Length cell hover gets the release-length sentence only', () => {
	assert.equal(issues.lengthFlagText({ source_flags: [LINK, LENGTH] }), LENGTH.message);
	assert.equal(issues.lengthFlagText({ source_flags: [LINK] }), null);
});
