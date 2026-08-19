import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LATENCY-03: the instrument that proves the LATENCY-01 fix.
 *
 * `scheduled_offset_ms` is the single number that regressed (measured 220.0ms
 * on 48/48 samples before the fix) and the single number that proves it moved.
 * It is logged through the existing always-on perf event ring, so
 * `window.__mdtPerfLog()` is all a browser agent or an e2e case needs.
 *
 * The failure mode this file exists to prevent is a DECORATIVE instrument. The
 * schedule time a call site asks for is not the schedule time the WebAudio call
 * receives: `_scheduleDeckSerial` re-clamps it up to `minimumSafeWhen` and may
 * pull it back down to a still-safe pending boundary. An offset logged from the
 * requested value would read 8ms while the clamp silently held the real
 * schedule at 128ms - the guard would pass while the thing it guards was
 * broken. So the emit must sit downstream of EVERY clamp, and the logged number
 * must move when the clamp moves.
 *
 * Clamp order in _scheduleDeckSerial, and where the emit sits relative to it:
 *   1. minimumSafeWhen = safeTransportScheduleTime(currentTime, latencySec)
 *   2. safeRequestedWhen = Math.max(when, minimumSafeWhen)
 *   3. effectiveWhen = supersedingScheduleTime(safeRequestedWhen, pending, minimumSafeWhen)
 *   -> emit reads effectiveWhen, which is the same const passed to
 *      processor.schedule(effectiveWhen, ...). Nothing rewrites it in between.
 *
 * Regression lines:
 * - if the logged offset is derived from the requested time rather than the
 *   post-clamp effective time then the instrument reads 8ms while the schedule
 *   is really 128ms and every guard built on it is decorative
 * - if the logged offset does not grow when the clamp minimum grows then the
 *   instrument is not measuring the clamp at all
 * - if the emit stops using the same value handed to processor.schedule then
 *   the log describes a schedule that never happened
 * - if the device floors stop travelling with the sample then a number measured
 *   on one machine gets compared to one from another and means nothing
 */

let math;

before(async () => {
	math = await loadTypeScriptModule('src/lib/player/transport/schedule-math.ts');
});

/** One schedule, with every clamp already resolved by the caller. */
function stages(overrides = {}) {
	return math.scheduleOffsetStages({
		contextTimeSec: 10,
		requestedWhenSec: 10.128,
		effectiveWhenSec: 10.128,
		processorLatencySec: 0.12,
		baseLatencySec: 0.005805,
		outputLatencySec: 0.032,
		active: true,
		...overrides
	});
}

//-----------------------------------------------------------------------------
// the number itself
//-----------------------------------------------------------------------------

test('the logged offset is the gap from context time to the effective schedule', () => {
	const row = stages();
	assert.equal(row.scheduled_offset_ms, 128);
	assert.equal(row.processor_latency_ms, 120);
	assert.equal(
		row.safety_ms,
		8,
		'safety_ms is the part the schedule policy owns; the processor latency is not ' +
			'this policy to spend, so it must be reported separately rather than folded in'
	);
	assert.equal(row.active, 1);
	assert.equal(stages({ active: false }).active, 0);
});

test('every sample carries its own device floor, so it cannot be compared blind', () => {
	const row = stages();
	assert.equal(row.base_latency_ms, 5.805);
	assert.equal(row.output_latency_ms, 32);
});

test('the row is all finite numbers, so it survives the log JSON round trip', () => {
	const row = stages();
	for (const [name, value] of Object.entries(row)) {
		assert.equal(typeof value, 'number', `${name} must be a number for the stages contract`);
		assert.ok(Number.isFinite(value), `${name} must be finite, got ${value}`);
	}
	assert.deepEqual(JSON.parse(JSON.stringify(row)), row);
});

//-----------------------------------------------------------------------------
// the instrument must not be decorative
//-----------------------------------------------------------------------------

test('SABOTAGE: raising the clamp minimum raises the logged offset with it', () => {
	// Stand in for safeTransportScheduleTime returning a much larger floor. If
	// the logged number does not move with it, the instrument is decorative.
	const base = stages({ requestedWhenSec: 10.008, effectiveWhenSec: 10.008 });
	assert.equal(base.scheduled_offset_ms, 8);
	for (const clampedWhenSec of [10.05, 10.128, 10.22, 11]) {
		const clamped = stages({ requestedWhenSec: 10.008, effectiveWhenSec: clampedWhenSec });
		assert.equal(
			clamped.scheduled_offset_ms,
			Math.round((clampedWhenSec - 10) * 1e6) / 1000,
			`if the clamp moves the schedule to ${clampedWhenSec} and the log still reads ` +
				`${base.scheduled_offset_ms}ms then the instrument is measuring the request, ` +
				'not the schedule, and would pass while the transport was broken'
		);
		assert.ok(
			clamped.scheduled_offset_ms > base.scheduled_offset_ms,
			'the logged offset must be strictly sensitive to the clamp'
		);
	}
});

test('a clamp is visible as a gap, never as silence', () => {
	// The pre-fix shape: an 8ms request re-inflated to 220ms by the floor.
	const row = stages({ requestedWhenSec: 10.008, effectiveWhenSec: 10.22 });
	assert.equal(row.scheduled_offset_ms, 220);
	assert.equal(
		row.requested_offset_ms,
		8,
		'the request must still be recorded next to the effective time, so a clamp reads ' +
			'as a gap between two logged numbers rather than as a missing one'
	);
	assert.ok(row.requested_offset_ms < row.scheduled_offset_ms);
});

test('a schedule in the past is refused rather than logged as a negative latency', () => {
	assert.throws(() => stages({ effectiveWhenSec: 9.9 }), RangeError);
	assert.throws(() => stages({ contextTimeSec: Number.NaN }), RangeError);
	assert.throws(() => stages({ active: 'yes' }), TypeError);
});

//-----------------------------------------------------------------------------
// wiring: the emit must sit downstream of every clamp
//
// Which value the engine feeds the instrument is not something the module can
// return to a caller, so it is pinned as source text.
//-----------------------------------------------------------------------------

test('the engine feeds the instrument the post-clamp time, not the requested one', () => {
	const body = engineBlockAfter(`async function _scheduleDeckSerial(
	deck: DeckId,
	when: number,
	inputSec: number | ((effectiveWhen: number) => number),
	active: boolean,
	tempoRatio: number | undefined,
	masterTempoEnabled: boolean | undefined,
	loop: LoopState | null | undefined,
	keyShiftSemitones: number | undefined
): Promise<number> {`);

	assert.ok(
		body.includes('effectiveWhenSec: effectiveWhen'),
		'if the instrument is fed anything but effectiveWhen then it reports a schedule ' +
			'that never reached the WebAudio call'
	);
	assert.ok(
		body.includes('requestedWhenSec: when'),
		'the requested time must still be recorded so a clamp shows up as a gap'
	);

	// effectiveWhen must be the SAME value handed to the worklet, with no
	// reassignment between. A const declaration is what makes that provable.
	assert.ok(
		body.includes('const effectiveWhen = supersedingScheduleTime('),
		'if effectiveWhen stops being a const resolved by supersedingScheduleTime then it ' +
			'can be rewritten after the instrument reads it'
	);
	assert.ok(
		body.includes('await processor.schedule(\n\t\t\teffectiveWhen,'),
		'if processor.schedule stops receiving effectiveWhen verbatim then the logged ' +
			'number describes a different schedule than the one that happened'
	);

	// Ordering: the clamp resolves, then the instrument reads it.
	const clampAt = body.indexOf('const effectiveWhen = supersedingScheduleTime(');
	const emitAt = body.indexOf('scheduleOffsetStages({');
	const scheduleAt = body.indexOf('await processor.schedule(');
	assert.ok(clampAt !== -1 && emitAt !== -1 && scheduleAt !== -1);
	assert.ok(
		clampAt < emitAt,
		'if the instrument reads before the clamp resolves then it measures the request'
	);
	assert.ok(
		emitAt < scheduleAt,
		'the values must be captured before the worklet ack so the log describes the call'
	);
});

test('the log write stays off the click-to-audio path it measures', () => {
	const body = engineBlockAfter(`async function _scheduleDeckSerial(
	deck: DeckId,
	when: number,
	inputSec: number | ((effectiveWhen: number) => number),
	active: boolean,
	tempoRatio: number | undefined,
	masterTempoEnabled: boolean | undefined,
	loop: LoopState | null | undefined,
	keyShiftSemitones: number | undefined
): Promise<number> {`);
	const scheduleAt = body.indexOf('await processor.schedule(');
	const recordAt = body.indexOf("recordPerfTiming('transport-schedule'");
	assert.ok(recordAt !== -1, 'if the row is never recorded then nothing is measured at all');
	assert.ok(
		scheduleAt < recordAt,
		'recordPerfTiming does a JSON stringify and a localStorage write; running it BEFORE ' +
			'the schedule call would add latency to the very path this fix shortens'
	);
});

test('the audio graph stamps this machine device floors once at build', () => {
	const body = engineBlockAfter('function _ensureGraph(): AudioContext {');
	assert.ok(
		body.includes("recordPerfTiming('audio-context'"),
		'without the device floors on record, a scheduled_offset_ms from one machine gets ' +
			'compared to one from another and the comparison means nothing'
	);
	for (const field of ['sample_rate_hz', 'base_latency_ms', 'output_latency_ms']) {
		assert.ok(body.includes(field), `the device floor stamp must carry ${field}`);
	}
});
