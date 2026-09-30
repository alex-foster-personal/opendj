/**
 * Pin 6af63c5e9b7c / FB-20: operator-facing hover breakdown vocabulary.
 *
 * The buckets themselves are computed by the daemon and tested there
 * (tests/webui/test_feedback_comments_summary.py); this file covers only the
 * wording the controls render from them.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let summaryMod;
before(async () => {
	summaryMod = await loadTypeScriptModule('src/lib/rb/feedback-pin-operator-summary.ts');
});

test('pin 6af63c5e9b7c describePinOperatorSummary names sent to queue, in-progress, delegated, fixed, merged', () => {
	const line = summaryMod.describePinOperatorSummary({
		total: 4,
		sent_to_queue: 1,
		in_progress: 2,
		delegated: 1,
		fixed: 0,
		merged: 0,
		blocked: 0,
		harvested: 0
	});
	assert.match(line, /sent to queue/i);
	assert.match(line, /in-progress/i);
	assert.match(line, /delegated/i);
	assert.match(line, /fixed/i);
	assert.match(line, /merged/i);
});

test('pin 6af63c5e9b7c describePinOperatorSummary shows blocked/harvested only when nonzero', () => {
	const base = {
		total: 3,
		sent_to_queue: 0,
		in_progress: 0,
		delegated: 0,
		fixed: 0,
		merged: 0,
		blocked: 0,
		harvested: 0
	};
	assert.doesNotMatch(summaryMod.describePinOperatorSummary(base), /blocked|harvested/);
	const line = summaryMod.describePinOperatorSummary({ ...base, blocked: 2, harvested: 1 });
	assert.match(line, /2 blocked, 1 harvested$/);
});

test('pin 6af63c5e9b7c describeFleetCorrelation speaks only when correlation did not run', () => {
	assert.equal(summaryMod.describeFleetCorrelation('ok'), null);
	assert.match(summaryMod.describeFleetCorrelation('ledger_missing'), /no progress ledger/);
	assert.match(summaryMod.describeFleetCorrelation('ledger_unreadable'), /did not parse/);
});
