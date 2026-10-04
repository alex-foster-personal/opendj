// requirement: LATENCY-01
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LATENCY-01: a plain play/pause must not pay the beat-sync alignment margin.
 *
 * The defect the maintainer reported as "play/pause feels slow" was a single constant:
 * SYNC_SCHEDULE_SAFETY_S (100ms), sized so several decks can agree on ONE
 * shared launch instant, was charged to every transport mutation including the
 * ones with nothing to align against. Add the output latency and the button
 * sat ~100-150ms behind the click, against a 30ms p99 budget.
 *
 * These tests pin the SPLIT, in both directions: plain transport schedules at
 * TRANSPORT_IMMEDIATE_SAFETY_S, and the 100ms is provably ABSENT from those
 * paths; beat-sync alignment still pays the 100ms and is provably UNCHANGED.
 * Sync pays for sync; plain transport does not.
 *
 * Regression lines:
 * - if a plain play/pause schedule time exceeds currentTime + latency + the
 *   immediate safety then transport pays a margin it does not owe and the button
 *   feels slow again
 * - if the plain-transport margin exceeds the LATENCY-01 30ms budget then the
 *   contract is broken before an e2e run even measures it
 * - if safeSyncScheduleTime stops defaulting to SYNC_SCHEDULE_SAFETY_S then
 *   beat-synced decks launch too close to now and land out of phase
 * - if the play() plain branches name SYNC_SCHEDULE_SAFETY_S again then the
 *   100ms is back on the click path
 * - if _synchronizeFollowers stops routing through safeSyncScheduleTime /
 *   commonSyncScheduleTimes then group alignment is no longer computed from one
 *   shared instant
 */

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

/** LATENCY-01: audible effect <=30ms p99 from the input event. The scheduling
 * margin is only one term in that budget, so it must sit well inside it. */
const AUDIBLE_BUDGET_MS = 30;

let constants;
let math;

before(async () => {
	constants = await loadTypeScriptModule('src/lib/player/constants.ts');
	math = await loadTypeScriptModule('src/lib/player/transport/schedule-math.ts');
});

/** Source text of a player module, positively located. Mirrors the discipline
 * in engine-source.mjs: a guard that cannot find its file must fail rather than
 * assert against ''. schedule-math is not on ENGINE_SOURCE_PATHS, so it is read
 * here rather than through engineBlockAfter. */
function playerSource(relativePath) {
	const text = readFileSync(`${SRC}/${relativePath}`, 'utf8');
	assert.ok(
		text.trim().length > 0,
		`if ${relativePath} reads empty then this guard silently asserts nothing`
	);
	return text;
}

//-----------------------------------------------------------------------------
// the two margins are distinct, and the plain one is small
//-----------------------------------------------------------------------------

test('the immediate-transport safety and the beat-sync margin are separate constants', () => {
	assert.equal(constants.SYNC_SCHEDULE_SAFETY_S, 0.1);
	assert.equal(constants.TRANSPORT_IMMEDIATE_SAFETY_S, 0.008);
	assert.ok(
		constants.TRANSPORT_IMMEDIATE_SAFETY_S > 0,
		'a zero margin would schedule in the past, which the render quantum ignores'
	);
	assert.ok(
		constants.TRANSPORT_IMMEDIATE_SAFETY_S < constants.SYNC_SCHEDULE_SAFETY_S,
		'if the plain margin is not smaller than the sync margin then nothing was fixed'
	);
});

test('the immediate-transport margin fits inside the LATENCY-01 audible budget', () => {
	assert.ok(
		constants.TRANSPORT_IMMEDIATE_SAFETY_S * 1000 <= AUDIBLE_BUDGET_MS,
		`if the plain margin exceeds ${AUDIBLE_BUDGET_MS}ms then scheduling alone ` +
			'blows the Class A budget before dispatch is even counted'
	);
	assert.ok(
		constants.SYNC_SCHEDULE_SAFETY_S * 1000 > AUDIBLE_BUDGET_MS,
		'the sync margin is deliberately outside the instant budget - if it were not, ' +
			'this whole split would be pointless'
	);
});

//-----------------------------------------------------------------------------
// plain transport: the 100ms is provably absent
//-----------------------------------------------------------------------------

test('a plain transport schedule lands at currentTime + latency + immediate safety', () => {
	const now = 10;
	const latency = 0.02;
	const when = math.safeTransportScheduleTime(now, latency);
	assert.ok(
		Math.abs(when - (now + latency + constants.TRANSPORT_IMMEDIATE_SAFETY_S)) < 1e-12,
		`plain transport must schedule at now + latency + immediate safety, got ${when}`
	);
});

test('the beat-sync 100ms is provably absent from every plain transport schedule', () => {
	// Sweep the realistic space rather than one lucky point: the claim is that
	// NO plain schedule anywhere pays the sync margin.
	for (const now of [0, 0.5, 10, 1234.5]) {
		for (const latency of [0, 0.005, 0.0116, 0.02, 0.05, 0.2]) {
			const when = math.safeTransportScheduleTime(now, latency);
			const syncWhen = now + latency + constants.SYNC_SCHEDULE_SAFETY_S;
			assert.ok(
				when < syncWhen,
				`if a plain schedule at now=${now} latency=${latency} reaches ${syncWhen} ` +
					'then it is still paying the beat-sync margin'
			);
			assert.ok(
				(when - now - latency) * 1000 <= AUDIBLE_BUDGET_MS,
				`plain schedule margin at now=${now} latency=${latency} must fit the ` +
					`${AUDIBLE_BUDGET_MS}ms budget, got ${(when - now - latency) * 1000}ms`
			);
		}
	}
});

test('a caller that genuinely needs the sync margin still passes it explicitly', () => {
	const when = math.safeTransportScheduleTime(10, 0.2, constants.SYNC_SCHEDULE_SAFETY_S);
	assert.ok(
		Math.abs(when - 10.3) < 1e-12,
		'the opt-in path must be unchanged, otherwise sync-critical callers lost their margin'
	);
});

test('the not-in-the-past guard is still a guard: zero and negatives are refused', () => {
	assert.throws(() => math.safeTransportScheduleTime(10, 0.2, 0), /safetySec/);
	assert.throws(() => math.safeTransportScheduleTime(10, -0.1), /processorLeadSec/);
	assert.throws(() => math.safeTransportScheduleTime(-1, 0.1), /nowContextTime/);
	assert.throws(() => math.safeTransportScheduleTime(Number.NaN, 0.1), /nowContextTime/);
});

//-----------------------------------------------------------------------------
// beat sync: the 100ms is provably retained
//-----------------------------------------------------------------------------

test('beat-sync alignment still defaults to the full 100ms margin', () => {
	const now = 10;
	const maxLatency = 0.2;
	const syncAt = math.safeSyncScheduleTime(now, maxLatency, now);
	assert.ok(
		Math.abs(syncAt - (now + maxLatency + constants.SYNC_SCHEDULE_SAFETY_S)) < 1e-12,
		`if beat sync stops paying ${constants.SYNC_SCHEDULE_SAFETY_S}s then followers ` +
			'can miss the shared launch instant and land audibly out of phase'
	);
	assert.ok(
		syncAt > math.safeTransportScheduleTime(now, maxLatency),
		'the sync launch must stay strictly further out than a plain transport schedule'
	);
});

test('a beat-sync group still launches every participant at one shared instant', () => {
	const syncAt = math.safeSyncScheduleTime(10, 0.2, 10);
	assert.deepEqual(math.commonSyncScheduleTimes(syncAt, 3), [syncAt, syncAt, syncAt]);
	assert.throws(() => math.commonSyncScheduleTimes(syncAt, 0), /participant/i);
});

test('the master-ready floor still carries the sync margin, not the immediate one', () => {
	// masterReadyContextTime dominates: the group must still clear it by 100ms.
	const syncAt = math.safeSyncScheduleTime(10, 0.01, 12);
	assert.ok(
		Math.abs(syncAt - (12 + constants.SYNC_SCHEDULE_SAFETY_S)) < 1e-12,
		`master-ready floor must clear by the sync margin, got ${syncAt}`
	);
});

//-----------------------------------------------------------------------------
// source guards: which default each function carries
//
// A default parameter is a value the module never returns to a caller, so a
// behavioural test can be satisfied by the RIGHT number arriving for the WRONG
// reason (a caller passing it explicitly). These pin the declaration.
//-----------------------------------------------------------------------------

test('safeTransportScheduleTime declares the immediate safety as its default', () => {
	const text = playerSource('lib/player/transport/schedule-math.ts');
	const at = text.indexOf('export function safeTransportScheduleTime(');
	assert.notEqual(at, -1, 'if safeTransportScheduleTime moved then this guard is pointed at nothing');
	const signature = text.slice(at, text.indexOf('): number {', at));
	assert.ok(
		signature.includes('safetySec = TRANSPORT_IMMEDIATE_SAFETY_S'),
		'if plain transport defaults to anything but the immediate safety then pause, ' +
			'seek, cue and loop inherit whatever margin it is instead'
	);
	assert.ok(
		!signature.includes('SYNC_SCHEDULE_SAFETY_S'),
		'if the plain-transport default names the sync margin then the defect is back'
	);
});

test('safeSyncScheduleTime declares the sync margin as its default safety', () => {
	const text = playerSource('lib/player/transport/schedule-math.ts');
	const at = text.indexOf('export function safeSyncScheduleTime(');
	assert.notEqual(at, -1, 'if safeSyncScheduleTime moved then this guard is pointed at nothing');
	const signature = text.slice(at, text.indexOf('): number {', at));
	assert.ok(
		signature.includes('safetySec = SYNC_SCHEDULE_SAFETY_S'),
		'if beat-sync alignment stops defaulting to the 100ms margin then group launches ' +
			'lose the handshake room they were sized for'
	);
});

//-----------------------------------------------------------------------------
// source guards: the engine's transport call sites
//-----------------------------------------------------------------------------

// e9493db18 (fix(rescue): schedule simultaneous play restore at shared context time, #2704)
// added startAtContextSec to play() and forcedWhen to schedulePlainTransport, so the
// play() and schedulePlainTransport anchors in this section moved with those signatures.
test('the two non-sync play branches schedule through the plain-transport helper', () => {
	const body = engineBlockAfter('	async play(deck: DeckId, pressT0Ms?: number, startAtContextSec?: number): Promise<void> {');
	assert.ok(
		!body.includes('SYNC_SCHEDULE_SAFETY_S'),
		'if play() names SYNC_SCHEDULE_SAFETY_S again then the branch that becomes master, ' +
			'or the branch where sync is explicitly off, is paying 100ms for nothing'
	);
	const helperUses = body.split('safeTransportScheduleTime(').length - 1;
	assert.equal(
		helperUses,
		1,
		'if play() defines more than one plain-transport margin then the branches can drift apart'
	);
	const branchUses = body.split('await schedulePlainTransport();').length - 1;
	assert.ok(
		branchUses >= 2,
		'if fewer than two non-sync play branches call schedulePlainTransport then one path ' +
			'bypasses the shared plain-transport helper'
	);
	assert.ok(
		body.includes('_synchronizeFollowers('),
		'if the beat-sync join branch is gone then a follower deck no longer phase-locks'
	);
});

test('pause, cue and seek schedule through the plain-transport horizon', () => {
	for (const anchor of [
		'	async pause(deck: DeckId, pressT0Ms?: number): Promise<void> {',
		'	async pressCue(deck: DeckId, pressT0Ms?: number): Promise<void> {',
		'	async quantizedSeek(deck: DeckId, ms: number, skipGridQuantize = false, pressT0Ms?: number, jumpBeats?: number | null): Promise<void> {'
	]) {
		const body = engineBlockAfter(anchor);
		assert.ok(
			!body.includes('SYNC_SCHEDULE_SAFETY_S'),
			`if ${anchor.trim()} names the sync margin then that Class A control is slow again`
		);
	}
});

test('the plain schedule horizon is the shared helper, not a hand-rolled margin', () => {
	const body = engineBlockAfter('function _futureScheduleTime(deck: DeckId): number {');
	assert.ok(
		body.includes('safeTransportScheduleTime('),
		'if _futureScheduleTime stops using the shared helper then pause, seek, cue, loop ' +
			'and key-shift each inherit whatever margin it hardcodes'
	);
	assert.ok(
		!body.includes('SYNC_SCHEDULE_SAFETY_S'),
		'if _futureScheduleTime names the sync margin then every plain transport mutation ' +
			'that routes through it pays 100ms'
	);
});

test('LATENCY-02 engine.play stays free of QUANTIZED LAUNCH branching', () => {
	const playBody = engineBlockAfter('async play(deck: DeckId, pressT0Ms?: number, startAtContextSec?: number): Promise<void> {');
	assert.doesNotMatch(playBody, /QUANTIZED LAUNCH/);
	assert.doesNotMatch(playBody, /armQuantizedLaunch/);
	const scheduleBody = engineBlockAfter('const schedulePlainTransport = async (forcedWhen?: number): Promise<void> => {');
	assert.doesNotMatch(scheduleBody, /QUANTIZED LAUNCH/);
	assert.doesNotMatch(scheduleBody, /armQuantizedLaunch/);
});

test('beat-sync scheduling is untouched: one shared instant, sync-margin sourced', () => {
	const body = engineBlockAfter(
		'async function _synchronizeFollowers(\n' +
			'\tmaster: DeckId,\n' +
			'\tfollowers: readonly DeckId[],\n' +
			'\toptions: _SyncOptions = {}\n' +
			'): Promise<void> {'
	);
	assert.ok(
		body.includes('safeSyncScheduleTime('),
		'if follower scheduling stops using safeSyncScheduleTime then the group launch ' +
			'instant is no longer computed with the alignment margin'
	);
	assert.ok(
		body.includes('commonSyncScheduleTimes('),
		'if participants stop sharing one schedule time then they can land out of phase'
	);
	assert.ok(
		body.includes('pendingSyncWaitTarget('),
		'the pending-wait is what keeps a superseding pending schedule from backdating a ' +
			'sync launch; without it, lowering the plain floor could pull a sync schedule in'
	);
});
