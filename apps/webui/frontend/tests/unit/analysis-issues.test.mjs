import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// H19 - never light an Err dot for an analysis kind that has no real detector.
// Beatgrid is the only kind with one today; the other eight slots must stay
// off rather than fabricate a state. The detector itself is heavily tested;
// the mapping layer that decides which dots light was not.
//
// Regression lines:
// - if a row with no rb_meta produces any issue then a dot lights on nothing
// - if a null beatgrid_issue produces an issue then "no problem" reads as a problem
// - if any kind other than beatgrid appears then a detector was invented
// - if the issue detail loses the disagreement BPM or the timestamp then the
//   dot is a bare colour the DJ cannot act on

let issues;
let jobs;

before(async () => {
	issues = await loadTypeScriptModule('src/lib/rb/analysis-issues.ts');
	jobs = await loadTypeScriptModule('src/lib/rb/job-progress.svelte.ts');
});

const REAL_ISSUE = {
	severity: 'warning',
	at_sec: 132.4,
	field_bpm: 128.0,
	interval_bpm: 124.7,
	disagreement_bpm: 3.3
};

// ------------------------------------------------------ nothing detected

test('a row with no rb_meta lights no Err dot at all', () => {
	assert.deepEqual(issues.analysisIssuesFor({}), {});
	assert.deepEqual(issues.analysisIssuesFor({ rb_meta: null }), {});
	assert.deepEqual(issues.analysisIssuesFor({ rb_meta: undefined }), {});
});

test('an explicitly clean beatgrid lights no Err dot', () => {
	assert.deepEqual(issues.analysisIssuesFor({ rb_meta: { beatgrid_issue: null } }), {});
	assert.deepEqual(issues.analysisIssuesFor({ rb_meta: { beatgrid_issue: undefined } }), {});
});

test('a fully analysed, fully healthy row lights no Err dot', () => {
	// The shape a real /rb-meta payload has: plenty of completed analysis, no
	// findings. Completion must never be read as an issue signal.
	const row = {
		stable_id: 'a'.repeat(40),
		rb_meta: {
			stable_id: 'a'.repeat(40),
			analysis_available: true,
			beatgrid_issue: null,
			cue_count: 8
		}
	};
	assert.deepEqual(issues.analysisIssuesFor(row), {});
});

// -------------------------------------------------- exactly one detector

test('a real beatgrid issue lights exactly one slot, and only that slot', () => {
	const map = issues.analysisIssuesFor({ rb_meta: { beatgrid_issue: REAL_ISSUE } });

	assert.deepEqual(Object.keys(map), ['beatgrid']);
	assert.equal(map.beatgrid.severity, 'warning');
});

test('every analysis kind without a detector stays off for any row shape', () => {
	const undetected = jobs.ANALYSIS_DOT_SLOTS.filter(
		(kind) => kind !== null && !issues.DETECTED_ANALYSIS_KINDS.includes(kind)
	);
	assert.equal(undetected.length, 8, `expected 8 detector-less slots, got ${undetected.length}`);

	const rows = [
		{},
		{ rb_meta: null },
		{ rb_meta: { beatgrid_issue: null } },
		{ rb_meta: { beatgrid_issue: REAL_ISSUE } },
		{ rb_meta: { beatgrid_issue: { ...REAL_ISSUE, severity: 'error' } } },
		// Fields a future detector might add: present in the payload, still no
		// detector wired, so still no dot.
		{ rb_meta: { beatgrid_issue: null, vocals_issue: REAL_ISSUE, key_issue: REAL_ISSUE } }
	];
	for (const row of rows) {
		const map = issues.analysisIssuesFor(row);
		for (const kind of undetected) {
			assert.equal(
				map[kind],
				undefined,
				`${kind} has no detector but lit for ${JSON.stringify(row)} - that is a guessed error state`
			);
		}
	}
});

test('the detected-kind list is the single place a new detector is declared', () => {
	assert.deepEqual([...issues.DETECTED_ANALYSIS_KINDS], ['beatgrid']);
	for (const kind of issues.DETECTED_ANALYSIS_KINDS) {
		assert.ok(
			jobs.ANALYSIS_DOT_SLOTS.includes(kind),
			`${kind} claims a detector but has no Err slot to light`
		);
	}
});

// -------------------------------------------------- the finding is actionable

test('the issue detail carries the numeric disagreement and where it happens', () => {
	const map = issues.analysisIssuesFor({ rb_meta: { beatgrid_issue: REAL_ISSUE } });
	const detail = map.beatgrid.detail;

	assert.match(detail, /3\.3 BPM/, `detail must name the disagreement, got ${detail}`);
	assert.match(detail, /t=132s/, `detail must name where it happens, got ${detail}`);
	assert.match(detail, /Beat Sync/, 'detail must say why the DJ should care');
});

test('severity is carried through rather than flattened to one colour', () => {
	const warning = issues.analysisIssuesFor({ rb_meta: { beatgrid_issue: REAL_ISSUE } });
	const error = issues.analysisIssuesFor({
		rb_meta: { beatgrid_issue: { ...REAL_ISSUE, severity: 'error' } }
	});

	assert.equal(warning.beatgrid.severity, 'warning');
	assert.equal(error.beatgrid.severity, 'error');
	for (const severity of ['warning', 'error']) {
		assert.equal(
			typeof jobs.ANALYSIS_ISSUE_COLORS[severity],
			'string',
			`severity ${severity} has no colour to render`
		);
	}
});
