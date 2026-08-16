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

let audio;

before(async () => {
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts');
});

function _observe(timeline, contextTime) {
	return audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime, performanceTime: contextTime * 1000 },
		DURATION_SEC
	);
}

function _schedule(timeline, revision, startContextTime, startPositionSec, active) {
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision,
		active,
		loop: null,
		startContextTime,
		startPositionSec,
		tempoRatio: 1
	});
}

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
