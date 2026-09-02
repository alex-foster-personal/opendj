import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * waveform-freezes-on-stale-output-timestamp: a presentation clock that has
 * stopped must SAY it has stopped.
 *
 * Wed 2 Sep 2026, 13:22-13:42 CEST: the waveform and playhead froze for twenty
 * minutes while audio played normally and the rAF loop ran at FULL rate (45%
 * of WebContent's main-thread time was inside rAF callbacks). Under the
 * CoreAudio overload storm, WebKit's HAL-sourced `getOutputTimestamp()
 * .contextTime` went stale while the render-thread sample clock kept
 * advancing. `observePresentedTransportTimeline` has three stale paths -
 * contextTime 0, contextTime regressed, and contextTime an exact repeat - and
 * ALL THREE leave the painted position untouched while `anyTransport` stays
 * true, so the loop re-arms forever and repaints an identical frame.
 *
 * THE CONTRACT ASSERTED HERE IS DETECTION, NOT FALLBACK.
 *
 * The alternative contract - fall back to `ctx.currentTime` and keep the
 * position moving - was considered and deliberately NOT asserted. presentation
 * .ts states as an invariant that context/output time is presentation truth
 * and that the position painted must trail the audio by output latency;
 * switching which clock is transport authority is a design decision about what
 * the operator is looking at, not a bug fix, and a blind test has no business
 * making it. Detection is strictly weaker, strictly safer, and is the thing
 * that was actually missing: the app had no way to know.
 *
 * The stall is measurable from the arguments the function ALREADY receives.
 * `performanceTime` keeps advancing while `contextTime` is stuck (WebKit
 * extrapolates it), so "performanceTime advanced by more than the stall window
 * while contextTime did not advance at all" is a complete, pure decision
 * needing no new dependency, no timer, and no clock injection. The observation
 * carries it; the engine escalates it.
 *
 * DEVICE-AGNOSTIC BY CONSTRUCTION: the input is two numbers per frame. No
 * device, no driver, no product.
 *
 * Regression lines:
 * - if a repeated contextTime is not reported as a stall then a frozen
 *   playhead over live audio is invisible to the app that is painting it
 * - if a stall is reported while the clock is advancing then the signal is
 *   noise and will be ignored when it matters
 * - if a non-finite performanceTime throws then a value the module itself
 *   calls diagnostic can kill the transport display permanently
 * - if authority does not return when the output timestamp recovers then one
 *   hiccup leaves the playhead permanently leading the audible position
 */

let audio;

before(async () => {
	audio = await loadTypeScriptModule('src/lib/player/transport/presentation.ts');
});

//-----------------------------------------------------------------------------
// a playing timeline, and a way to advance the two clocks independently
//-----------------------------------------------------------------------------

/** A timeline with an active schedule from contextTime 1, position 0. */
function playingTimeline() {
	const timeline = audio.createPresentedTransportTimeline(0);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: null,
		startContextTime: 1,
		startPositionSec: 0,
		tempoRatio: 1
	});
	return timeline;
}

/**
 * Drive N frames at 60Hz, choosing what the output timestamp reports each time.
 *
 * `contextTimeAt` models the clock that stalls; the performance clock always
 * advances, because in the live incident it did - that is precisely why the
 * stall is detectable at all.
 */
function driveFrames(
	timeline,
	frames,
	contextTimeAt,
	{ durationSec = 600, sampleClockAt = null, fromFrame = 0 } = {}
) {
	const observations = [];
	for (let frame = fromFrame; frame < fromFrame + frames; frame += 1) {
		observations.push(
			audio.observePresentedTransportTimeline(
				timeline,
				{ contextTime: contextTimeAt(frame), performanceTime: 10_000 + frame * (1000 / 60) },
				durationSec,
				sampleClockAt === null ? undefined : sampleClockAt(frame)
			)
		);
	}
	return observations;
}

/**
 * The render-thread clock, which kept advancing throughout the live incident.
 *
 * Starts at 2s (where the fixtures below have the output clock when it dies)
 * and runs at real time, one 60Hz frame per step.
 */
function sampleClockAt(frame) {
	return 2 + frame / 60;
}

/**
 * The stall window in ms.
 *
 * Read from the module when it declares one, so the tests below cannot drift
 * from the shipped threshold. The fallback exists ONLY so the two controls
 * still exercise real behaviour before the constant lands - a control that
 * fails for want of a constant proves nothing about the code it is guarding.
 * The constant's absence is asserted on its own, once, below.
 */
const DEFAULT_STALL_MS = 500;

function stallWindowMs() {
	return typeof audio.PRESENTATION_STALL_MS === 'number'
		? audio.PRESENTATION_STALL_MS
		: DEFAULT_STALL_MS;
}

/** Frames needed to exceed the stall window at 60Hz, plus a margin. */
function framesToOutlast(ms) {
	return Math.ceil(ms / (1000 / 60)) + 5;
}

test('the stall window is a declared constant, not a number in a test', () => {
	assert.equal(
		typeof audio.PRESENTATION_STALL_MS,
		'number',
		'if presentation.ts declares no stall window then broken - the threshold that ' +
			'decides "the playhead has frozen" cannot live only in a test file, or the app ' +
			'and its guard are free to disagree about what a freeze is'
	);
});

function assertStallReported(observations, scenario) {
	const stalled = observations.filter((o) => o.clock_stalled === true);
	assert.ok(
		stalled.length >= 1,
		`if ${scenario} then broken - the waveform freezes over live audio and the app has ` +
			'no idea, which is exactly the twenty minutes of Wed 2 Sep 2026 13:22-13:42 CEST'
	);
}

/**
 * Drive one of the three stale paths and assert the whole contract on it.
 *
 * Every stale path gets the identical treatment, because the operator cannot
 * tell them apart and should not have to: whichever way the output clock fails,
 * the playhead keeps moving and the app says the clock is untrusted.
 */
function assertStaleClockHandled(scenario, contextTimeAt) {
	const timeline = playingTimeline();
	// One good frame so output has definitely started, then the clock fails.
	driveFrames(timeline, 1, () => 2, { sampleClockAt });
	const frames = framesToOutlast(stallWindowMs());
	const stale = driveFrames(timeline, frames, contextTimeAt, {
		sampleClockAt,
		fromFrame: 1
	});

	assertStallReported(stale, scenario);

	const during = stale.filter((o) => o.clock_stalled === true);
	assert.ok(
		during.every((o) => o.clock_source === 'sample'),
		`if the position during a ${scenario} stall still comes from the output clock then ` +
			'broken - that clock is the thing that stopped, so the playhead freezes'
	);
	const positions = during.map((o) => o.position_sec);
	assert.ok(
		positions.length >= 2 && positions[positions.length - 1] > positions[0],
		`if the presented position does not advance during a ${scenario} stall then broken - ` +
			'the waveform is frozen over live audio, which is the whole defect'
	);
	assert.ok(
		during.every((o) => o.accepted),
		'a frame the fallback painted must report as accepted, or the rAF loop stands down ' +
			'and the fallback paints exactly once'
	);
	return timeline;
}

test('a contextTime that repeats unchanged keeps the playhead moving, and says so', () => {
	assertStaleClockHandled('repeated contextTime', () => 2);
});

test('a contextTime that returns to 0 after output started keeps the playhead moving', () => {
	assertStaleClockHandled('contextTime pinned at 0', () => 0);
});

test('a contextTime that regresses keeps the playhead moving', () => {
	assertStaleClockHandled('regressing contextTime', () => 1.5);
});

test('authority returns to the output timestamp the moment it recovers', () => {
	const timeline = assertStaleClockHandled('repeated contextTime', () => 2);
	// The device comes back. The output clock resumes from where it really is,
	// which is BEHIND the sample clock by the output latency - the recovery must
	// be judged against the last value the output clock was trusted at, not
	// against how far the fallback ran.
	const recovered = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 2.5, performanceTime: 99_000 },
		600,
		sampleClockAt(400)
	);
	assert.equal(
		recovered.clock_stalled,
		false,
		'if the stall verdict sticks after the output clock advances then broken - the ' +
			'indicator would stay lit for the rest of the session'
	);
	assert.equal(
		recovered.clock_source,
		'output',
		'if authority does not return to the output timestamp then broken - the playhead ' +
			'would lead the audible position permanently after one hiccup'
	);
	// The schedule starts at contextTime 1 / position 0 at 1x, so an output clock
	// reading 2.5 is position 1.5. The fallback had run the position well past
	// that; snapping back is the POINT - it is the playhead ceasing to lead.
	assert.ok(
		Math.abs(recovered.position_sec - 1.5) < 0.001,
		`if the recovered position is ${recovered.position_sec} rather than the 1.5 the ` +
			'output clock reports then broken - the fallback would have left a permanent ' +
			'offset behind it'
	);
});

test('detection still works with no sample clock to fall back to', () => {
	// A caller that cannot supply ctx.currentTime must degrade to detection, not
	// to silence. This is also what keeps every pre-existing 3-argument call site
	// behaving exactly as it did.
	const timeline = playingTimeline();
	driveFrames(timeline, 1, () => 2);
	const stale = driveFrames(timeline, framesToOutlast(stallWindowMs()), () => 2, {
		fromFrame: 1
	});
	assertStallReported(stale, 'a stall with no sample clock available reports nothing');
	assert.ok(
		stale.every((o) => o.clock_source === 'output'),
		'if a fallback is claimed with no clock to fall back to then broken'
	);
});

//-----------------------------------------------------------------------------
// the controls: it must not cry wolf
//-----------------------------------------------------------------------------

test('CONTROL: a healthy advancing clock is never reported as stalled', () => {
	const timeline = playingTimeline();
	const healthy = driveFrames(timeline, 240, (frame) => 2 + frame / 60);
	assert.ok(
		healthy.every((o) => o.accepted),
		'if a normally advancing clock is rejected then this control proves nothing'
	);
	assert.equal(
		new Set(healthy.map((o) => o.position_sec)).size,
		240,
		'a healthy clock paints a new position every frame'
	);
	assert.ok(
		healthy.every((o) => o.clock_source === 'output'),
		'a healthy clock must never hand authority to the fallback'
	);
	assert.equal(
		healthy.filter((o) => o.clock_stalled === true).length,
		0,
		'if a healthy advancing clock reports a stall then broken - the operator learns to ' +
			'ignore the signal before the real freeze arrives'
	);
});

test('CONTROL: a stall shorter than the window is not reported', () => {
	const timeline = playingTimeline();
	driveFrames(timeline, 1, () => 2);
	// Deliberately half the window: a single dropped frame or a scheduler hiccup
	// must not raise an alarm, or every loaded moment becomes an incident.
	const brief = driveFrames(timeline, Math.floor(framesToOutlast(stallWindowMs()) / 2), () => 2, {
		sampleClockAt,
		fromFrame: 1
	});
	assert.equal(
		brief.filter((o) => o.clock_stalled === true).length,
		0,
		'if a sub-window stall is reported then broken - one late frame on a busy machine ' +
			'would fire the alarm continuously'
	);
});

//-----------------------------------------------------------------------------
// the diagnostic field must not be able to kill the transport
//-----------------------------------------------------------------------------

test('a non-finite or negative performanceTime does not throw', () => {
	// presentation.ts:261 - "Performance time is correlation/diagnostic data,
	// never transport authority" - and yet :216-221 throws a RangeError on it.
	// A throw here escapes _tick, which has already nulled _rafId and has no
	// catch, so the presentation clock dies permanently while audio plays on.
	// WebKit EXTRAPOLATES performanceTime, so a bad value is a live possibility.
	for (const performanceTime of [Number.NaN, -1, Number.POSITIVE_INFINITY]) {
		const timeline = playingTimeline();
		assert.doesNotThrow(
			() =>
				audio.observePresentedTransportTimeline(
					timeline,
					{ contextTime: 2, performanceTime },
					600
				),
			`if performanceTime=${String(performanceTime)} throws then broken - a field the ` +
				'module itself calls diagnostic can permanently kill the rAF loop that paints ' +
				'the waveform, while audio carries on'
		);
	}
});

test('a bad performanceTime still lets the presented position advance', () => {
	const timeline = playingTimeline();
	const first = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 2, performanceTime: Number.NaN },
		600
	);
	const second = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 3, performanceTime: Number.NaN },
		600
	);
	assert.ok(
		second.position_sec > first.position_sec,
		'if a bad performanceTime freezes the position then broken - contextTime advanced ' +
			'by a full second and the transport authority is contextTime, not performanceTime'
	);
});

test('a bad performanceTime yields "not known", never a claimed stall', () => {
	const timeline = playingTimeline();
	const observations = driveFrames(timeline, 1, () => 2);
	assert.ok(observations[0].accepted);
	const blind = [];
	for (let frame = 0; frame < framesToOutlast(stallWindowMs()); frame += 1) {
		try {
			blind.push(
				audio.observePresentedTransportTimeline(
					timeline,
					{ contextTime: 2, performanceTime: Number.NaN },
					600
				)
			);
		} catch (error) {
			assert.fail(
				'if an unreadable performanceTime throws instead of degrading to "not known" ' +
					`then broken - ${String(error)}`
			);
		}
	}
	assert.ok(
		blind.every((o) => o.clock_stalled !== true),
		'if a stall is CLAIMED while the clock used to measure it is unreadable then broken ' +
			'- "I cannot tell" and "it has stalled" are different answers, and collapsing ' +
			'them raises alarms on evidence that does not exist'
	);
});

//-----------------------------------------------------------------------------
// contextTime keeps failing fast, because it IS transport authority
//-----------------------------------------------------------------------------

test('CONTROL: a non-finite or negative contextTime still throws', () => {
	// The asymmetry is the point. performanceTime is diagnostic and must degrade;
	// contextTime decides where the playhead is and must never be guessed at.
	for (const contextTime of [Number.NaN, -1]) {
		const timeline = playingTimeline();
		assert.throws(
			() =>
				audio.observePresentedTransportTimeline(
					timeline,
					{ contextTime, performanceTime: 1000 },
					600
				),
			RangeError,
			`if contextTime=${String(contextTime)} is accepted then broken - the playhead ` +
				'would be computed from a value that is not a time'
		);
	}
});
