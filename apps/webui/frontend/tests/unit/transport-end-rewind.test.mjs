/**
 * Regression: a deck that plays to its natural end, is rewound, and is played
 * again must become audible from the rewound position.
 *
 * Live symptom (2026-08-07, DDJ-400 set): finished tracks "play only briefly
 * before going quiet", or produce no sound at all after a rewind.
 *
 * These drive the exported presented-transport timeline through the exact
 * sequence the engine performs, so the failure is reproducible without an
 * AudioContext.
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const DURATION_SEC = 10;

/** The loop the WebKit artifact test engages, and the continuity rule it now
 * asserts over it. Mirrors LOOP_* in tests/e2e/webkit-deckload.spec.ts. */
const LOOP = { in_ms: 400, out_ms: 1_600, engaged: true, beat_length: null };
const LOOP_LENGTH_MS = LOOP.out_ms - LOOP.in_ms;
const LOOP_OBSERVE_MS = 5_000;
const LOOP_SAMPLE_INTERVAL_MS = 100;
const CONTINUITY = {
	sliceMs: 1_000,
	loopLengthMs: LOOP_LENGTH_MS,
	minimumAdvanceMs: 250
};

let audio;
let continuity;

before(async () => {
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts');
	continuity = await loadTypeScriptModule('tests/e2e/support/loop-continuity.ts');
});

function _observe(timeline, contextTime) {
	return audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime, performanceTime: contextTime * 1000 },
		DURATION_SEC
	);
}

function _schedule(timeline, revision, startContextTime, startPositionSec, active, loop = null) {
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision,
		active,
		loop,
		startContextTime,
		startPositionSec,
		tempoRatio: 1
	});
}

/**
 * Sample the presented playhead the way the artifact test's page loop does:
 * a wall-clock cadence, reading whatever position the presentation projects.
 */
function _sampleLoop(timeline, { fromContextTime, sinceMs, forMs, everyMs }) {
	const samples = [];
	for (let elapsedMs = 0; elapsedMs <= forMs; elapsedMs += everyMs) {
		samples.push({
			observedAtMs: sinceMs + elapsedMs,
			positionMs: _observe(timeline, fromContextTime + elapsedMs / 1000).position_sec * 1000
		});
	}
	return samples;
}

/** The rule this PR retired: the spread of the phases the sampler happened to
 * see. Kept here so both of its failure modes stay pinned by assertions. */
function _retiredSpreadRuleAccepts(samples) {
	const positions = samples.map((sample) => sample.positionMs);
	return Math.max(...positions) - Math.min(...positions) > LOOP_LENGTH_MS / 4;
}

test('sparse presentation samples cannot count loop wraps from backward edges', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	_schedule(timeline, 1, 1, 0, true, LOOP);

	// Both samples are real output-presentation projections. Four 1.2s wraps
	// happen between them, but their phases move from 700ms to 900ms, so a test
	// that counts only decreasing adjacent positions would observe zero wraps.
	const samples = [1.7, 6.7].map((contextTime) => _observe(timeline, contextTime).position_sec);
	for (const positionSec of samples) {
		assert.ok(
			positionSec >= LOOP.in_ms / 1000 && positionSec < LOOP.out_ms / 1000,
			`presented loop position must remain in range, got ${positionSec}s`
		);
	}
	const backwardEdges = samples.filter((positionSec, index) => index > 0 && positionSec < samples[index - 1]);
	assert.equal(backwardEdges.length, 0, 'sparse samples do not expose every real wrap');
	assert.deepEqual(samples.map((positionSec) => Math.round(positionSec * 1000)), [700, 900]);

	// Those two healthy positions span 200ms, so the retired spread rule would
	// have called this loop broken. The continuity rule reads elapsed time, not
	// phase spread, and refuses to judge an observation this starved at all
	// rather than blaming the deck for the sampler's gaps.
	const sparse = [
		{ observedAtMs: 700, positionMs: 700 },
		{ observedAtMs: 5_700, positionMs: 900 }
	];
	assert.equal(_retiredSpreadRuleAccepts(sparse), false, 'the retired rule rejected a healthy loop');
	assert.throws(
		() => continuity.findStalledLoopSlices(sparse, CONTINUITY),
		/observer must sample faster than the slice/,
		'a starved observation is an error, never a silent pass or a fake stall'
	);
});

test('a loop sampled for its whole window advances in every one-second slice', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	_schedule(timeline, 1, 1, 0, true, LOOP);

	const samples = _sampleLoop(timeline, {
		fromContextTime: 1.5,
		sinceMs: 0,
		forMs: LOOP_OBSERVE_MS,
		everyMs: LOOP_SAMPLE_INTERVAL_MS
	});
	for (const { positionMs } of samples) {
		assert.ok(
			positionMs > LOOP.in_ms - 1 && positionMs < LOOP.out_ms + 1,
			`presented loop position must remain in range, got ${positionMs}ms`
		);
	}

	const slices = continuity.sliceLoopContinuity(samples, CONTINUITY);
	assert.equal(slices.length, 5, 'a 5s observation holds five one-second slices');
	assert.deepEqual(
		continuity.findStalledLoopSlices(samples, CONTINUITY),
		[],
		'a healthy loop never stalls'
	);
	// Each slice accounts for its own second of wall clock, wraps included, so
	// the verdict never depends on how many phases the sampler happened to see.
	for (const slice of slices) {
		assert.ok(
			Math.abs(slice.advanceMs - CONTINUITY.sliceMs) < 1,
			`slice advance should track wall clock, got ${slice.advanceMs}ms`
		);
	}
});

test('a loop that stalls late in the window fails even though its spread is wide', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	_schedule(timeline, 1, 1, 0, true, LOOP);

	// Two seconds of real looping, then the deck parks at loop-out: the stall
	// and the clamp the artifact test could not previously tell from playback.
	const moving = _sampleLoop(timeline, {
		fromContextTime: 1.5,
		sinceMs: 0,
		forMs: 2_000,
		everyMs: LOOP_SAMPLE_INTERVAL_MS
	});
	_schedule(timeline, 2, 3.55, LOOP.out_ms / 1000, false, LOOP);
	const parked = _sampleLoop(timeline, {
		fromContextTime: 3.6,
		sinceMs: 2_100,
		forMs: 2_900,
		everyMs: LOOP_SAMPLE_INTERVAL_MS
	});
	const samples = [...moving, ...parked];
	assert.equal(
		new Set(parked.map((sample) => sample.positionMs)).size,
		1,
		'the parked half must present one frozen cursor'
	);

	// The retired rule passes this: the first two seconds alone spread wider
	// than a quarter of the loop, and nothing after them was ever required.
	assert.equal(_retiredSpreadRuleAccepts(samples), true, 'the retired rule accepted a stalled deck');

	const stalled = continuity.findStalledLoopSlices(samples, CONTINUITY);
	assert.ok(stalled.length >= 2, `expected the stalled seconds to be reported, got ${stalled.length}`);
	const lastSlice = stalled.at(-1);
	assert.equal(
		lastSlice.endedAtMs,
		samples.at(-1).observedAtMs,
		'slices are anchored at the end of the observation, so a tail stall cannot hide'
	);
	assert.equal(lastSlice.advanceMs, 0, 'a frozen cursor advances by nothing');
	assert.match(
		continuity.describeStalledLoopSlices(stalled),
		/advanced 0ms over \d+ samples \(0 moved\)/,
		'the failure message must name the stalled slice'
	);
});

test('a small backward step is clock noise, not a wrap worth crediting', () => {
	assert.equal(continuity.wrapAwareAdvanceMs(700, 900, LOOP_LENGTH_MS), 200);
	// A real wrap: near the top of the window back to near the bottom.
	assert.equal(continuity.wrapAwareAdvanceMs(1_550, 450, LOOP_LENGTH_MS), 100);
	// A 2ms twitch of a frozen cursor is NOT 1198ms of progress.
	assert.equal(continuity.wrapAwareAdvanceMs(900, 898, LOOP_LENGTH_MS), 0);
	assert.equal(continuity.wrapAwareAdvanceMs(900, 900, LOOP_LENGTH_MS), 0);
	assert.throws(
		() => continuity.wrapAwareAdvanceMs(900, 1_000, 0),
		/loopLengthMs must be > 0/,
		'the loop length is required - no hidden default'
	);
});

test('a continuity slice longer than the loop is refused rather than guessed', () => {
	const samples = [
		{ observedAtMs: 0, positionMs: 700 },
		{ observedAtMs: 1_300, positionMs: 700 }
	];
	assert.throws(
		() => continuity.findStalledLoopSlices(samples, { ...CONTINUITY, sliceMs: 1_300 }),
		/must be shorter than loopLengthMs/,
		'a slice longer than the loop makes its advance ambiguous'
	);
});

/** Play from 0, run past the end, then apply the engine's natural-end stop. */
function _playToNaturalEnd(timeline) {
	_schedule(timeline, 1, 1, 0, true);
	const atEnd = _observe(timeline, 1 + DURATION_SEC + 0.5);
	assert.equal(atEnd.audible, false, 'deck must go inaudible at the natural end');
	// Engine natural-end handler: revisioned stop parked at the duration.
	_schedule(timeline, 2, 1 + DURATION_SEC + 0.6, DURATION_SEC, false);
	const stopped = _observe(timeline, 1 + DURATION_SEC + 0.7);
	assert.equal(stopped.transport_pending, false, 'natural-end stop must present');
	return 1 + DURATION_SEC + 0.7;
}

test('finished deck rewound to 0 and replayed becomes audible from the rewind point', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	const afterEnd = _playToNaturalEnd(timeline);

	audio.setPausedTransportTimelineCursor(timeline, 0, DURATION_SEC);
	assert.equal(timeline.paused_position_sec, 0, 'rewind must move the paused cursor to 0');

	_schedule(timeline, 3, afterEnd + 0.1, 0, true);
	const playing = _observe(timeline, afterEnd + 1.1);

	assert.equal(playing.audible, true, 'replay after rewind must be audible');
	assert.ok(
		playing.position_sec > 0.5 && playing.position_sec < 1.5,
		`replay position should advance from 0, got ${playing.position_sec}`
	);
});

test('finished deck rewound mid-track and replayed stays audible past the old end', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	const afterEnd = _playToNaturalEnd(timeline);

	audio.setPausedTransportTimelineCursor(timeline, DURATION_SEC / 2, DURATION_SEC);
	_schedule(timeline, 3, afterEnd + 0.1, DURATION_SEC / 2, true);

	const playing = _observe(timeline, afterEnd + 1.1);
	assert.equal(playing.audible, true, 'replay from mid-track must be audible');
	assert.ok(
		playing.position_sec > DURATION_SEC / 2,
		`replay position should advance past the rewind point, got ${playing.position_sec}`
	);
});

test('a lagging presentation clock forces the scheduled seek path', () => {
	const idle = {
		playing: false,
		audible: false,
		controlActive: false,
		presentationPending: false,
		pendingScheduleCount: 0,
		scheduleIntentCount: 0
	};
	assert.equal(
		audio.transportNeedsScheduledMutation(idle),
		false,
		'a fully idle deck seeks via the paused cursor'
	);
	assert.equal(
		audio.transportNeedsScheduledMutation({ ...idle, presentationPending: true }),
		true,
		'presentation lag alone must route the seek through the scheduler'
	);
	assert.throws(
		() => audio.transportNeedsScheduledMutation({ ...idle, presentationPending: undefined }),
		/presentationPending must be boolean/,
		'the flag is required - no hidden default'
	);
});

test('rewind is rejected while the natural-end stop is still unpresented', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	_schedule(timeline, 1, 1, 0, true);
	_observe(timeline, 1 + DURATION_SEC + 0.5);
	// Stop acknowledged but never observed: desired_revision leads presented.
	_schedule(timeline, 2, 1 + DURATION_SEC + 0.6, DURATION_SEC, false);

	assert.throws(
		() => audio.setPausedTransportTimelineCursor(timeline, 0, DURATION_SEC),
		/paused transport cursor cannot move while audio is active or pending/,
		'an unpresented stop must block the paused-cursor rewind'
	);
});
