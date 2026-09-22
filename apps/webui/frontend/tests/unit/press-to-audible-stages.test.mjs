// requirement: LATENCY-03
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import {
	engineBlockAfter,
	readFrontendSource as readSource,
	SCHEDULE_DECK_SERIAL_ANCHOR
} from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Q1 / LATENCY-01 / S2: the press-to-audible half nobody has ever seen.
 *
 * `scheduled_offset_ms` measures from the instant `_scheduleDeckSerial` reads
 * `_ctx.currentTime`. Everything BEFORE that read is invisible today, and that
 * is the expensive half: the ScopedCommandScheduler wait (play claims
 * [deck, 'sync'], so an idle deck's play can queue behind another deck's seek),
 * `_resumeContext()`, and the deck's own `rt.scheduleTail`. A transport row
 * reading 52.4ms while the operator waited 300ms for the button is the failure
 * mode this instrument exists to end.
 *
 * Two new stages close it:
 *   press_to_schedule_ms  = clock read time - the input stamp
 *   input_to_audible_ms   = press_to_schedule_ms + scheduled_offset_ms
 *
 * Regression lines:
 * - if input_to_audible_ms is not sensitive to BOTH terms then the queue wait
 *   is back to being invisible and the P0 number is decorative again
 * - if the press stamp is taken after the command scheduler wait then it
 *   measures the engine and not the button
 * - if the press stages appear on rows with no press stamp then a
 *   sync/automation schedule gets quoted as if a human had pressed something
 * - if a negative or non-finite delta is logged rather than refused then the
 *   ratchet can be passed by a broken clock
 */

let math;

before(async () => {
	math = await loadTypeScriptModule('src/lib/player/transport/schedule-math.ts');
});

/** The shipping ROUND 2 shape: 120ms self-report, 46.8ms lead, 54.8ms offset. */
function stages(overrides = {}) {
	return math.scheduleOffsetStages({
		contextTimeSec: 10,
		requestedWhenSec: 10.0548,
		effectiveWhenSec: 10.0548,
		processorLeadSec: 0.0468,
		processorLatencySec: 0.12,
		baseLatencySec: 0.005805,
		outputLatencySec: 0.032,
		active: true,
		...overrides
	});
}

//-----------------------------------------------------------------------------
// the two new numbers
//-----------------------------------------------------------------------------

test('a schedule with no press stamp keeps exactly the stages it had before', () => {
	const row = stages();
	assert.equal(row.press_to_schedule_ms, undefined);
	assert.equal(row.input_to_audible_ms, undefined);
	assert.equal(
		row.scheduled_offset_ms,
		54.8,
		'the pre-existing ratchet number must be untouched by this addition'
	);
});

test('a pressed schedule reports the queue wait and the modeled audible instant', () => {
	const row = stages({ pressToScheduleMs: 12.5 });
	assert.equal(row.press_to_schedule_ms, 12.5);
	assert.equal(
		row.input_to_audible_ms,
		67.3,
		'input_to_audible_ms is the whole S2 budget: the wait to reach the audio ' +
			'clock PLUS the offset the schedule was placed at'
	);
	assert.equal(row.scheduled_offset_ms, 54.8, 'the offset term must not be rewritten');
});

test('SABOTAGE: input_to_audible_ms is sensitive to the queue wait, not just the offset', () => {
	// The whole point of Q1. If this number only tracks scheduled_offset_ms then
	// a play that waited 300ms behind another deck's seek still reads 54.8ms and
	// the P0 budget passes while the button feels broken.
	const base = stages({ pressToScheduleMs: 0 });
	assert.equal(base.input_to_audible_ms, 54.8);
	for (const waitMs of [1, 12.5, 120, 300]) {
		const row = stages({ pressToScheduleMs: waitMs });
		assert.equal(
			row.input_to_audible_ms,
			Math.round((base.input_to_audible_ms + waitMs) * 1000) / 1000,
			`a ${waitMs}ms command-queue wait must move input_to_audible_ms by ${waitMs}ms`
		);
		assert.ok(row.input_to_audible_ms > base.input_to_audible_ms);
	}
});

test('SABOTAGE: input_to_audible_ms is sensitive to the schedule offset too', () => {
	// The mirror failure: a number that only tracks the press wait would hide a
	// clamp re-inflating the schedule to 220ms.
	const base = stages({ pressToScheduleMs: 10 });
	const clamped = stages({
		pressToScheduleMs: 10,
		requestedWhenSec: 10.008,
		effectiveWhenSec: 10.22
	});
	assert.equal(clamped.scheduled_offset_ms, 220);
	assert.equal(clamped.input_to_audible_ms, 230);
	assert.ok(clamped.input_to_audible_ms > base.input_to_audible_ms);
});

test('a broken clock is refused rather than logged as a negative latency', () => {
	assert.throws(() => stages({ pressToScheduleMs: -0.5 }), RangeError);
	assert.throws(() => stages({ pressToScheduleMs: Number.NaN }), RangeError);
	assert.throws(() => stages({ pressToScheduleMs: Number.POSITIVE_INFINITY }), RangeError);
	// Zero is legal: a press that reached the audio clock inside the same tick.
	assert.doesNotThrow(() => stages({ pressToScheduleMs: 0 }));
});

test('an explicitly undefined press stamp behaves like an absent one', () => {
	// The engine passes the value through unconditionally, so `undefined` is the
	// shape a sync-path or automation schedule actually arrives in.
	const row = stages({ pressToScheduleMs: undefined });
	assert.equal(row.press_to_schedule_ms, undefined);
	assert.equal(row.input_to_audible_ms, undefined);
});

test('the pressed row is all finite numbers, so it survives the log JSON round trip', () => {
	const row = stages({ pressToScheduleMs: 12.5 });
	for (const [name, value] of Object.entries(row)) {
		assert.equal(typeof value, 'number', `${name} must be a number for the stages contract`);
		assert.ok(Number.isFinite(value), `${name} must be finite, got ${value}`);
	}
	assert.deepEqual(JSON.parse(JSON.stringify(row)), row);
});

test('the press stages are appended, so the existing [perf] line prefix is unchanged', () => {
	// tests/e2e/webkit-deckload.spec.ts parses `[perf] kind k=v k=v` positionally
	// only in the sense that it splits on whitespace, but humans and the ring
	// summary read left to right: keep the ratchet number first.
	const keys = Object.keys(stages({ pressToScheduleMs: 12.5 }));
	assert.equal(keys[0], 'scheduled_offset_ms');
	assert.deepEqual(keys.slice(-3), [
		'press_to_schedule_ms',
		'input_to_audible_ms',
		'input_to_output_ms'
	]);
	assert.deepEqual(Object.keys(stages()), keys.slice(0, -3));
});

//-----------------------------------------------------------------------------
// wiring: the stamp must be taken at the INPUT, not at the engine
//-----------------------------------------------------------------------------

test('the UI command boundary stamps the press before anything can queue', () => {
	const ipc = readSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.ok(
		ipc.includes('pressT0Ms: number = performance.now()'),
		'if runPerformanceCommandFromUi stops defaulting the stamp to its own entry then ' +
			'every UI press measures from wherever the engine happened to start'
	);
	const stampAt = ipc.indexOf('export async function runPerformanceCommandFromUi(');
	const runAt = ipc.indexOf('_commandScheduler.run(scopes, run)');
	assert.ok(stampAt !== -1 && runAt !== -1);
	assert.ok(
		ipc.includes('return _dispatchUnknown(command, _currentCommandSession(), pressT0Ms);'),
		'the stamp must reach the dispatcher, or the scheduler wait it exists to expose ' +
			'is measured from after the wait'
	);
	assert.ok(
		ipc.includes("await engine.play(command.deck, pressT0Ms)") &&
			ipc.includes('await engine.pause(command.deck, pressT0Ms)') &&
			ipc.includes('await engine.pressCue(command.deck, pressT0Ms)'),
		'play, pause and cue are the three P0 press paths; a stamp that stops at ' +
			'_execute measures nothing'
	);
});

test('agent-native parity: the browser IPC can carry the same press stamp', () => {
	// House rule: every UI interaction needs a programmatic equivalent. Without
	// this, a browser agent driving `dispatch` could never produce the two
	// stages a human press produces, so the P0 number would be unmeasurable by
	// exactly the automation that is supposed to measure it.
	const ipc = readSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.ok(
		ipc.includes('dispatch(message: unknown, pressT0Ms?: number): Promise<PerformanceState>;'),
		'the PerformanceBrowserIpc contract must expose the stamp'
	);
	assert.ok(
		ipc.includes('_dispatchUnknown(message, commandGeneration, _validatedPressStamp(pressT0Ms))'),
		'and the installed dispatch must actually forward it'
	);
	assert.ok(
		ipc.includes('function _validatedPressStamp('),
		'a stamp crossing the IPC boundary is untrusted input and must be validated like ' +
			'every command field; a NaN would silently drop the stages from a row that still ' +
			'looks complete'
	);
});

test('the engine turns the stamp into a delta at the same clock read it reports', () => {
	const body = engineBlockAfter(SCHEDULE_DECK_SERIAL_ANCHOR);

	const clockAt = body.indexOf('const scheduleContextTime = _ctx.currentTime;');
	const deltaAt = body.indexOf('const pressToScheduleMs = measurePressToScheduleMs(');
	const emitAt = body.indexOf('scheduleOffsetStages({');
	assert.ok(clockAt !== -1, 'the audio clock read is the reference point for the press delta');
	assert.ok(deltaAt !== -1, 'if the delta is never computed then the press half stays invisible');
	assert.ok(
		clockAt < deltaAt && deltaAt < emitAt,
		'the delta must be taken immediately after the same currentTime read the row ' +
			'reports, or the two halves of input_to_audible_ms describe different instants'
	);
	assert.ok(
		body.includes('pressToScheduleMs'),
		'the delta must reach scheduleOffsetStages, not just be computed'
	);
	// The delta must be read BEFORE the worklet round trip, which is the part
	// scheduled_offset_ms already covers; measuring it after would double-count.
	const scheduleAt = body.indexOf('await processor.schedule(');
	assert.ok(deltaAt < scheduleAt);
});

test('an implausible press delta is recorded loudly and dropped, never logged', () => {
	const helper = engineBlockAfter(`export function measurePressToScheduleMs(
	pressT0Ms: number | undefined,
	deck: PerfDeck
): number | undefined {`);
	assert.ok(
		helper.includes('recordPerfEvent('),
		'a stamp from a clock that ran backwards means the instrument has gone blind; ' +
			'dropping it silently is how a blind instrument keeps reporting green'
	);
	assert.ok(
		helper.includes('return undefined;'),
		'the transport path must survive a bad stamp - an instrument may never break audio'
	);
});

test('the three press paths forward their stamp into the schedule', () => {
	// e9493db18 (#2704) added startAtContextSec to play(); the anchor follows the signature.
	for (const anchor of [
		'async play(deck: DeckId, pressT0Ms?: number, startAtContextSec?: number): Promise<void> {',
		'async pause(deck: DeckId, pressT0Ms?: number): Promise<void> {',
		'async pressCue(deck: DeckId, pressT0Ms?: number): Promise<void> {'
	]) {
		const body = engineBlockAfter(anchor);
		assert.ok(
			body.includes('pressT0Ms'),
			`${anchor} accepts a press stamp and must hand it to _schedulePress, or the ` +
				'parameter is decorative'
		);
	}
});
