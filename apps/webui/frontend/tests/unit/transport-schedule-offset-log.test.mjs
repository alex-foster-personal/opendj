// requirement: LATENCY-03
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

/**
 * One schedule, with every clamp already resolved by the caller.
 *
 * The shipping ROUND 2 shape: a 120ms processor self-report, a 46.8ms
 * onset-ramp lead derived from it (0.39x), and the 8ms immediate safety on top,
 * i.e. a 54.8ms scheduled offset.
 */
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
// the number itself
//-----------------------------------------------------------------------------

test('the logged offset is the gap from context time to the effective schedule', () => {
	const row = stages();
	assert.equal(row.scheduled_offset_ms, 54.8);
	assert.equal(row.processor_latency_ms, 120);
	assert.equal(
		row.processor_lead_ms,
		46.8,
		'the lead the floor actually charged must be logged separately from the ' +
			'self-report, or the 78ms round 2 gave back is invisible in the record'
	);
	assert.equal(
		row.safety_ms,
		8,
		'safety_ms is the part the schedule policy owns, i.e. the offset with the ' +
			'processor LEAD removed. Subtracting the self-report instead would make it ' +
			'read -65.2ms, which sails through a "<= 30ms" ceiling while meaning nothing'
	);
	assert.equal(row.active, 1);
	assert.equal(stages({ active: false }).active, 0);
});

test('SABOTAGE: a lead larger than the self-report is refused, not logged', () => {
	// The inversion this guards: passing latency where the lead belongs (or a
	// ramp factor above 1) puts the schedule back at 128ms while every field
	// still looks plausible. safety_ms alone would not catch it - it would read
	// exactly 8ms again, because the offset moved with the lead.
	assert.throws(
		() =>
			stages({
				requestedWhenSec: 10.128,
				effectiveWhenSec: 10.128,
				// The two arguments swapped: the ramp fraction reported as the
				// self-report, the block charged as the lead.
				processorLeadSec: 0.12,
				processorLatencySec: 0.0468
			}),
		/exceeds the self-report/
	);
	assert.throws(() => stages({ processorLeadSec: -0.001 }), RangeError);
	// The boundary itself is legal: a processor whose ramp IS its whole latency.
	assert.doesNotThrow(() =>
		stages({
			requestedWhenSec: 10.128,
			effectiveWhenSec: 10.128,
			processorLeadSec: 0.12,
			processorLatencySec: 0.12
		})
	);
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
	keyShiftSemitones: number | undefined,
	pressT0Ms: number | undefined,
	reanchorGeneration?: number
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
	keyShiftSemitones: number | undefined,
	pressT0Ms: number | undefined,
	reanchorGeneration?: number
): Promise<number> {`);
	const scheduleAt = body.indexOf('await processor.schedule(');
	// Q1 made the kind a value rather than a literal, because a pressed
	// schedule now files under `transport-schedule-press` so a pitch-fader drag
	// cannot evict it. The write itself is what this test guards, so it matches
	// the call and asserts the kind is the press-aware one separately.
	const recordAt = body.indexOf('recordPerfTiming(row.kind,');
	assert.ok(recordAt !== -1, 'if the row is never recorded then nothing is measured at all');
	assert.ok(
		body.includes('const row = scheduleRowFacts(scheduleStages, _ctx.state, pressT0Ms);'),
		'the kind must be derived from the row being filed (scheduleRowFacts reads the ' +
			'press stamp out of the stages); hardcoding it back to one literal returns ' +
			'press rows to the fader-flooded bucket that used to evict them'
	);
	assert.ok(
		scheduleAt < recordAt,
		'recordPerfTiming does a JSON stringify and a localStorage write; running it BEFORE ' +
			'the schedule call would add latency to the very path this fix shortens'
	);
});

test('the audio graph stamps this machine device floors, carrying every term', () => {
	const body = engineBlockAfter(
		'export function stampContextDeviceFloors(ctx: AudioContext): void {'
	);
	assert.ok(
		body.includes("recordPerfTiming('audio-context'"),
		'without the device floors on record, a scheduled_offset_ms from one machine gets ' +
			'compared to one from another and the comparison means nothing'
	);
	for (const field of ['sample_rate_hz', 'base_latency_ms', 'output_latency_ms']) {
		assert.ok(body.includes(field), `the device floor stamp must carry ${field}`);
	}
});

test('the device-floor row is re-stamped once the context is actually running', () => {
	// AudioContext.outputLatency reads 0.00 while the context is SUSPENDED, and
	// a context is suspended at construction until a user gesture resumes it. A
	// build-only stamp therefore under-reports the device floor by the whole
	// output term, silently, and every later reader of that row inherits it.
	const helper = engineBlockAfter(
		'export function stampContextDeviceFloors(ctx: AudioContext): void {'
	);
	assert.ok(
		helper.includes("const running = ctx.state === 'running'"),
		'if the stamp does not read the context state then it cannot know whether the ' +
			'outputLatency it just recorded is a real floor or a suspended-context zero'
	);
	assert.ok(
		helper.includes('context_running'),
		'the row must say which reading it is, or a consumer cannot tell the ' +
			'authoritative stamp from the placeholder'
	);
	assert.ok(
		helper.includes('if (running && _stampedRunningContext === ctx) return;'),
		'the running stamp must be idempotent - both the statechange listener and ' +
			'_resumeContext can reach it, and a ring full of duplicate rows is noise'
	);

	const build = engineBlockAfter('function _ensureGraph(): AudioContext {');
	assert.ok(
		build.includes('stampContextDeviceFloors('),
		'the build-time stamp must survive: it is the only reading available if the ' +
			'context never runs'
	);
	assert.ok(
		helper.includes('if (running) _stampedRunningContext = ctx;'),
		'the stamp must be remembered against THIS context rather than as a sticky ' +
			"boolean, or a rebuilt graph keeps quoting the previous device's floors"
	);
	// The listener moved on Wed 2 Sep 2026: the engine's own statechange handler
	// acted on 'running' ONLY, which is how suspended/interrupted/closed fell
	// through in silence for ~24 minutes. There is now exactly one owner of the
	// event, and the re-stamp is a branch inside it. What this guard pins is
	// unchanged - that SOMETHING still listens - only where to look for it.
	assert.ok(
		build.includes('armAudioContextWatchdog('),
		'if the graph stops arming the context watchdog then nothing listens for the ' +
			'state transition, and a context resumed outside _resumeContext never gets ' +
			'its authoritative row'
	);
	// #2155 (Sat 12 Sep 2026, commit 1db624ef7) added a third `recreateGraph`
	// argument to armAudioContextWatchdog's signature for output-stall
	// recovery; the anchor below is re-pointed at the new signature rather
	// than deleted, per this file's own regression-line contract.
	const arming = engineBlockAfter(
		'export function armAudioContextWatchdog(\n\tctx: AudioContext,\n\tisAnyDeckPlaying: () => boolean,\n\trecreateGraph: (() => Promise<void>) | null = null\n): void {'
	);
	assert.ok(
		arming.includes("ctx.state === 'running'") && arming.includes('stampContextDeviceFloors(ctx)'),
		'the arming must still re-stamp on the transition to running, or the authoritative ' +
			'row is never taken for a context that starts suspended'
	);

	const resume = engineBlockAfter('async function _resumeContext(): Promise<AudioContext> {');
	assert.ok(
		resume.includes('stampContextDeviceFloors(ctx)'),
		'the resume path is the belt for the statechange listener; without it the ' +
			'authoritative row depends on one event firing'
	);
});
