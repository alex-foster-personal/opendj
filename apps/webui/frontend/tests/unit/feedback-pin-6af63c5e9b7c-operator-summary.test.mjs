/**
 * Pin 6af63c5e9b7c / FB-20: operator-facing hover breakdown vocabulary.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let summaryMod;
before(async () => {
	summaryMod = await loadTypeScriptModule('src/lib/rb/feedback-pin-operator-summary.ts');
});

test('pin 6af63c5e9b7c summarizePinOperatorBuckets maps the maintainer labels and progress-tree building', () => {
	const pins = [
		{ id: 'q', status: null, issue_url: null, agent_note: null },
		{ id: 'o', status: 'open', issue_url: null, agent_note: null },
		{ id: 'd', status: 'issued', issue_url: 'https://github.com/o/r/issues/4085', agent_note: null },
		{
			id: 'p',
			status: 'issued',
			issue_url: 'https://github.com/o/r/issues/99',
			agent_note: 'PARTIAL: remainder in #100'
		},
		{ id: 'f', status: 'fixed', issue_url: null, agent_note: null },
		{ id: 'm', status: 'merged', issue_url: null, agent_note: null }
	];
	const progressNodes = [
		{ status: 'building', links: { issues: ['4085'] } },
		{ status: 'partial', links: { issues: ['#100'] } }
	];
	const buckets = summaryMod.summarizePinOperatorBuckets(pins, progressNodes);
	assert.equal(buckets.total, 6);
	assert.equal(buckets.sent_to_queue, 1);
	assert.equal(buckets.delegated, 2);
	assert.equal(buckets.fixed, 1);
	assert.equal(buckets.merged, 1);
	assert.ok(buckets.in_progress >= 3, 'open, partial overlay, and fleet building all count in-progress');
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
