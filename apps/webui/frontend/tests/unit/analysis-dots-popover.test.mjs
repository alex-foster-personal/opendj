import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const SRC = new URL('../../src/', import.meta.url);
const dots = readFileSync(new URL('lib/components/rb/browser/AnalysisDots.svelte', SRC), 'utf8');

test('AnalysisDots uses one hover popover for coverage and Err modes', () => {
	assert.match(dots, /mode\?: 'coverage' \| 'issues'/);
	assert.match(dots, /role="dialog"/);
	assert.match(dots, /onpointerenter=\{openWithIntent\}/);
	assert.match(dots, /onkeydown=\{onKeydown\}/);
	assert.match(dots, /analysisStatus\(/);
	assert.match(dots, /ANALYSIS_COLORS/);
	assert.match(dots, /ANALYSIS_ISSUE_COLORS/);
});

test('AnalysisDots orders missing rows through the shared HTTP client', () => {
	assert.match(dots, /orderTrackAnalysis\(stableId, kind\)/);
	assert.match(dots, /jobProgress\.upsert\(/);
	assert.match(dots, /phase: 'queued'/);
	assert.match(dots, /getTrackAnalysisOrders\(stableId\)/);
});
