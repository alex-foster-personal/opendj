// requirement: AUTOPLAN-01, AUTOPLAN-03
// [if] the master is under the target [then] the decision is not due, [else stop].
// [if] the track is too short for a plan after the target [then] it decides early enough to fit one, [else stop].
// [if] the engine refuses or is unreachable [then] the answer is a reason, never an exception, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let early;

before(async () => {
	early = await loadTypeScriptModule('src/lib/rb/auto-play-early.ts');
});

test('the decision is due at the soft target on a long track', () => {
	assert.equal(
		early.decisionDueMs(420_000, 180_000, 128),
		180_000,
		'if a long track ignores the target then broken'
	);
});

test('a short track decides early enough to fit the longest common plan', () => {
	const lead = early.requiredLeadMs(128);
	assert.ok(
		lead > early.CUE_BUDGET_MS + early.PREPARE_BUDGET_MS + 55_000,
		`if the lead omits the plan then broken: ${lead}`
	);
	assert.equal(early.decisionDueMs(200_000, 180_000, 128), 200_000 - lead);
});

test('a track under the lead decides at once and never before zero', () => {
	assert.equal(early.decisionDueMs(60_000, 180_000, 128), 0);
});

test('an unknown length schedules nothing', () => {
	assert.equal(early.decisionDueMs(null, 180_000, 128), null);
	assert.equal(early.decisionDueMs(0, 180_000, 128), null);
});

test('an unknown tempo still sizes a lead', () => {
	assert.ok(Number.isFinite(early.requiredLeadMs(null)));
	assert.ok(early.requiredLeadMs(null) > early.CUE_BUDGET_MS + early.PREPARE_BUDGET_MS);
});

test('a window is never asked for inside the time it takes to get there', () => {
	assert.equal(early.earliestWindowMs(175_000, 180_000), 175_000 + early.MIN_REACH_MS);
	assert.equal(
		early.earliestWindowMs(100_000, 180_000),
		180_000,
		'if a window before the target is asked for then broken'
	);
});

test('the shortlist is distinct, in pick order, and stops when the picks run out', () => {
	const rows = ['a', 'b', 'c'];
	const pick = (taken) => rows.find((row) => !taken.has(row)) ?? null;
	assert.deepEqual(early.buildShortlist(pick, 2), ['a', 'b']);
	assert.deepEqual(early.buildShortlist(pick, 5), ['a', 'b', 'c']);
	assert.deepEqual(
		early.buildShortlist(() => 'a', 5),
		['a'],
		'if a repeating pick loops forever then broken'
	);
});

test('a close window shortens the wait for the render, and a far one is capped', () => {
	assert.equal(early.prepareWaitMs(60_000), early.PREPARE_BUDGET_MS);
	assert.equal(
		early.prepareWaitMs(23_000),
		23_000 - early.MIN_REACH_MS,
		'if the wait can outlast the window then broken'
	);
	assert.equal(early.prepareWaitMs(5_000), 0, 'if a wait goes negative then broken');
});

const REQUEST = {
	out_deck: 1,
	in_deck: 2,
	shortlist: ['t'],
	intent: 'smooth',
	recent_styles: [],
	earliest_ms: 1,
	target_ms: 1
};

test('an answer with a track carries its window', async () => {
	const body = {
		track: 't',
		style: 'cut',
		bars: 8,
		reason: 'r',
		window: { start_ms: 5, start_bar: 3 }
	};
	const answer = await early.requestDecision(REQUEST, async () => Response.json(body));
	assert.equal(answer.track, 't');
	assert.equal(answer.window.start_bar, 3);
});

test('every failure to decide is a reason', async () => {
	const refused = await early.requestDecision(
		REQUEST,
		async () => new Response('nope', { status: 500 })
	);
	assert.equal(refused.track, null);
	assert.match(refused.reason, /500/);
	const down = await early.requestDecision(REQUEST, async () => {
		throw new Error('connection refused');
	});
	assert.match(down.reason, /connection refused/);
	const hollow = await early.requestDecision(REQUEST, async () =>
		Response.json({ track: 't', reason: 'r' })
	);
	assert.equal(hollow.track, null, 'if a track with no window is trusted then broken');
});

test('the readout names the plan, and why there is none', () => {
	const base = {
		track: 'abcdefghijklmnop',
		style: 'cut',
		bars: 8,
		window: { start_bar: 97, label: 'outro' },
		due_ms: 180_000,
		reason: 'r'
	};
	assert.match(
		early.decisionLine({ ...base, phase: 'scheduled' }, true),
		/abcdefghijkl at bar 97 \(outro\), cut over 8 bars/
	);
	assert.match(
		early.decisionLine({ ...base, phase: 'fallback' }, true),
		/end-of-track handoff will run: r/
	);
	assert.match(
		early.decisionLine({ ...base, phase: 'idle', track: null, window: null, reason: null }, true),
		/Decides at 180 s/
	);
	assert.match(early.decisionLine({ ...base, phase: 'idle' }, false), /off/);
	assert.throws(() => early.decisionLine({ ...base, phase: 'nonsense' }, true), /unhandled/);
});
