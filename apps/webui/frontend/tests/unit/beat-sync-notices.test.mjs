import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/** @type {(planned: unknown[], planFailed: unknown[], master: number) => any[]} */
let beatSyncOutcomeNotices;
before(async () => {
	({ beatSyncOutcomeNotices } = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts'));
});

// Pin 9bf12adccb45: "temporarily lets remove the beatsync limitation for
// phase lock - just make it happen and accept the funk - still show toast
// but make it orange warning instead of red and blocking."
//
// beat-sync-math.test.mjs proves BAR now RETURNS a folded anchor instead of
// throwing. toast-behavior/toast-tray prove a 'warn' toast paints orange and
// logs at warn severity. This file covers the join between them: given a
// planned follower that folded, what does the engine actually say, at what
// severity, and what does it write to the perf ring.
//
// The engine's own call site is four lines (`for (const notice of
// beatSyncOutcomeNotices(...)) pushToast(...)`), which is why the decision
// was lifted out of it - the fixture library is 128/124/128 BPM and contains
// no 2:1 pair, so a fold cannot be produced end-to-end without changing the
// shared fixture (rejected in #1628's review).
//
// The function itself lives in beat-sync-math.ts, beside the plan types it
// reads, rather than in a module of its own: a separate file would add a
// 38th import to audio-engine.svelte.ts and a 41st importer to
// deck-slots.ts, both of which are live quality-gate ceilings that break
// other people's branches (see ops/quality/baseline.json's fan_in history).
// It is a pure function returning data, so the engine still owns every
// effect.

const bar = (tempoNormalization) => ({ mode: 'bar', tempoNormalization });
const beat = (tempoNormalization) => ({ mode: 'beat', tempoNormalization });

test('a BAR follower that folded to half tempo warns, and does not read as a failure', () => {
	const notices = beatSyncOutcomeNotices([{ deck: 2, plan: bar(0.5) }], [], 1);
	assert.equal(notices.length, 1);
	const [notice] = notices;
	assert.equal(notice.kind, 'warn', 'a lock that HAPPENED must not be raised in the error colour');
	assert.match(notice.message, /deck 2 at half tempo/);
	assert.match(notice.message, /phase holds, the bar count does not/);
	assert.equal(notice.groupKey, 'beat-sync-fold:1');
	assert.deepEqual(notice.events, [
		{ kind: 'beat-sync-fold', detail: 'tempoNormalization=0.5', deck: 2 }
	]);
});

test('a double-tempo fold says double, not half', () => {
	const [notice] = beatSyncOutcomeNotices([{ deck: 3, plan: bar(2) }], [], 4);
	assert.match(notice.message, /deck 3 at double tempo/);
	assert.equal(notice.events[0].detail, 'tempoNormalization=2');
	assert.equal(notice.groupKey, 'beat-sync-fold:4');
});

test('an exact BAR lock is silent - there is nothing surprising to report', () => {
	assert.deepEqual(beatSyncOutcomeNotices([{ deck: 2, plan: bar(1) }], [], 1), []);
});

test('BEAT mode never raises a fold notice, however its tempo was normalized', () => {
	// BEAT never promised a bar count, so a normalized BEAT lock is ordinary.
	assert.deepEqual(beatSyncOutcomeNotices([{ deck: 2, plan: beat(0.5) }], [], 1), []);
	assert.deepEqual(beatSyncOutcomeNotices([{ deck: 2, plan: beat(2) }], [], 1), []);
});

test('every folded deck gets its own perf row, and one toast names them all', () => {
	const [notice] = beatSyncOutcomeNotices(
		[
			{ deck: 2, plan: bar(0.5) },
			{ deck: 3, plan: bar(1) },
			{ deck: 4, plan: bar(2) }
		],
		[],
		1
	);
	assert.match(notice.message, /deck 2 at half tempo, deck 4 at double tempo/);
	assert.ok(!notice.message.includes('deck 3'), 'the exact-anchor deck is not a fold');
	assert.deepEqual(
		notice.events.map((e) => e.deck),
		[2, 4]
	);
});

test('a refused follower is still an error, and is a SEPARATE notice from a fold', () => {
	const notices = beatSyncOutcomeNotices(
		[{ deck: 2, plan: bar(0.5) }],
		[{ deck: 3, message: 'no phase-capable anchor' }],
		1
	);
	assert.deepEqual(
		notices.map((n) => n.kind),
		['warn', 'error'],
		'a fold and a refusal must not be flattened onto one severity'
	);
	const [, refusal] = notices;
	assert.match(refusal.message, /skipped deck\(s\) \[3\]/);
	assert.equal(refusal.groupKey, 'beat-sync-followers:1');
	assert.deepEqual(refusal.events, [
		{ kind: 'beat-sync-skip', detail: 'no phase-capable anchor', deck: 3 }
	]);
});

test('a clean sync says nothing at all', () => {
	assert.deepEqual(beatSyncOutcomeNotices([], [], 1), []);
});

test('a notice carries the severity its perf rows must be filed under', () => {
	// recordPerfEvent's severity argument DEFAULTS to 'warn', so a caller that
	// omits it files a refusal - a follower that never locked - as a warning
	// that can never escalate. The engine passes notice.kind, which means the
	// toast colour and the ring severity cannot drift apart; these two are the
	// values it passes.
	const [fold] = beatSyncOutcomeNotices([{ deck: 2, plan: bar(0.5) }], [], 1);
	assert.equal(fold.kind, 'warn');
	const [skip] = beatSyncOutcomeNotices([], [{ deck: 3, message: 'no anchor' }], 1);
	assert.equal(skip.kind, 'error', 'a refused follower must be filed at error severity');
});
