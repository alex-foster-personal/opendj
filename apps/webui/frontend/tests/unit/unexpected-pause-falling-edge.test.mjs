// requirement: AUDIOLIVE-10
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { engineBlockAfter, SCHEDULE_DECK_ANCHOR } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Issue #2153 watchdog: a deck paused and seeked to 0:00 at the same instant
 * raised "Deck 1 stopped at 0:01, before the decoded audio ends", 3 of 3 times
 * (Silver, Chrome on demon-llama, Fri 2 Oct 2026).
 *
 * Cause: pause() runs inside withPauseOrigin('command') and records ONE falling
 * edge. The seek that lands while that pause is still queued sees a pending
 * schedule, so it schedules too, carrying `active: rt.desiredActive`, which the
 * pause has already set to false. `_scheduleDeck` treated every `active: false`
 * call as a falling edge, read the origin outside any wrapper ('other'), took
 * the pre-pause position, and classified a paused deck as a track cut short.
 *
 * Regression lines:
 * - if a stop request onto an already-paused deck is classified then every seek,
 *   cue jump or sync re-anchor during a pause in flight is a false red toast
 * - if a genuine untagged mid-track stop of a PLAYING deck is no longer
 *   classified then a truncated file stops in silence again (the overshoot)
 * - if the CUE button's stop is untagged then pressing CUE on a playing deck
 *   reads as a track cut short
 */

let h;
let rows;

before(async () => {
	h = await loadTypeScriptModule('tests/unit/fixtures/unexpected-pause-report-entry.ts');
	h.subscribePerfEvents((row) => {
		if (row.kind === 'audio-unexpected-pause') rows.push(row);
	});
});

beforeEach(() => {
	rows = [];
	h.toasts.splice(0, h.toasts.length);
});

const midTrackStop = {
	origin: 'other',
	deck: 1,
	position_ms: 1_200,
	duration_ms: 300_000,
	metadata_duration_ms: null,
	processor_error: null,
	context_state: 'running'
};

function unexpectedPauseToasts() {
	return h.toasts.filter((toast) => /stopped at/.test(toast.message));
}

test('a stop request onto an already-paused deck records nothing and toasts nothing', () => {
	h.notePlayingFallingEdge({ ...midTrackStop, was_active: false });
	assert.equal(rows.length, 0, 'if a re-stated pause records a row then the seek-during-pause false alarm is back');
	assert.equal(unexpectedPauseToasts().length, 0);
});

test('CONTROL: an untagged mid-track stop of a playing deck is still flagged', () => {
	// The overshoot a "never classify" fix would pass: the watchdog must still
	// catch a deck that really did stop short.
	h.notePlayingFallingEdge({ ...midTrackStop, was_active: true });
	assert.equal(rows.length, 1);
	assert.match(rows[0].message, /cause=source-ended-early/);
	assert.equal(unexpectedPauseToasts().length, 1);
});

test('CONTROL: an operator pause of a playing deck is still not flagged', () => {
	h.withPauseOrigin('command', () =>
		h.notePlayingFallingEdge({ ...midTrackStop, origin: h.readPauseOrigin(), was_active: true })
	);
	assert.equal(rows.length, 0);
});

test('_scheduleDeck captures the standing intent BEFORE overwriting it, and passes it on', () => {
	const body = engineBlockAfter(SCHEDULE_DECK_ANCHOR);
	const capture = body.indexOf('const wasActive = rt.desiredActive;');
	const overwrite = body.indexOf('rt.desiredActive = active;');
	assert.ok(capture >= 0, 'if _scheduleDeck does not capture the prior intent the edge cannot be told apart');
	assert.ok(overwrite > capture, 'if the capture follows the overwrite then was_active always equals active');
	assert.match(body, /notePlayingFallingEdge\(\{\s*was_active: wasActive,/);
});

test("pressCue's stop of a playing deck is tagged as an operator command", () => {
	const body = engineBlockAfter('async pressCue(deck: DeckId, pressT0Ms?: number): Promise<void> {');
	assert.match(
		body,
		/withPauseOrigin\('command', \(\) =>\s*_schedulePress\(deck, _futureScheduleTime\(deck\), target \/ 1000, false, pressT0Ms\)/
	);
});
