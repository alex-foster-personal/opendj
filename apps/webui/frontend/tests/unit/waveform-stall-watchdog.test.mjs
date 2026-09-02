import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * waveform-freezes-on-stale-output-timestamp: the belt to the other braces.
 *
 * `presentation-clock-stall.test.mjs` asserts that the ONE diagnosed cause (a
 * stale `getOutputTimestamp().contextTime`) is detected where it happens.
 * `engine-tick-exception-safety.test.mjs` asserts that a throw in the tick is
 * survivable. This file asserts the thing that would have caught Wed 2 Sep
 * 2026 without anybody having diagnosed anything at all: **somebody watches
 * the number being painted.**
 *
 * That is the general form of both incidents recorded today. In each one, the
 * app held every fact it needed - the clock had stopped, the position was
 * identical frame after frame - and nobody looked. A watchdog on the painted
 * position catches a stalled output timestamp, a dead tick, a future cause
 * nobody has thought of yet, and any regression that reintroduces one.
 *
 * THIS MODULE DOES NOT EXIST YET. `src/lib/player/transport/presentation-stall
 * .ts` is the contract these tests define. Pure fold over samples, the same
 * shape as `xrun-math.ts` and for the same reason: the decision must be
 * testable without waiting out a real timer on a thrashing laptop.
 *
 * DEVICE-AGNOSTIC BY CONSTRUCTION: the input is `{playing, position_ms, tMs}`.
 *
 * Regression lines:
 * - if a position that has not moved for longer than the window while playing
 *   emits nothing then a frozen waveform over live audio is invisible again
 * - if a paused deck emits then the alarm fires whenever nobody is playing
 * - if a moving position emits then the alarm fires through a healthy set
 * - if one stall emits per sample then a freeze is a toast storm on top of a
 *   freeze
 */

const STALL_MODULE = 'src/lib/player/transport/presentation-stall.ts';

let stall = null;
let loadError = null;

before(async () => {
	try {
		stall = await loadTypeScriptModule(STALL_MODULE);
	} catch (error) {
		loadError = error;
	}
});

function _stall() {
	assert.equal(
		loadError,
		null,
		`if ${STALL_MODULE} does not exist then broken - nothing compares the position being ` +
			'painted against the position painted last frame, so a playhead frozen over live ' +
			'audio is something only the operator can notice, twenty minutes late ' +
			`(bundler said: ${loadError === null ? '' : String(loadError)})`
	);
	return stall;
}

/** Fold a run of samples at a fixed cadence and collect every verdict. */
function runSamples(samples, sampleMs = 16.67) {
	const mod = _stall();
	const verdicts = [];
	let state;
	let tMs = 0;
	for (const sample of samples) {
		state = mod.foldPresentationSample(state, { ...sample, tMs });
		if (state.verdict !== 'ok') verdicts.push({ tMs, verdict: state.verdict });
		tMs += sampleMs;
	}
	return { verdicts, state };
}

/** A run of frames long enough to span `ms`, all reporting the same position. */
function frozenFor(ms, { playing = true, positionMs = 12_345 } = {}, sampleMs = 16.67) {
	return Array.from({ length: Math.ceil(ms / sampleMs) + 2 }, () => ({
		playing,
		position_ms: positionMs
	}));
}

/** A run of frames where the position advances like a healthy transport. */
function movingFor(ms, { playing = true, fromMs = 0 } = {}, sampleMs = 16.67) {
	return Array.from({ length: Math.ceil(ms / sampleMs) + 2 }, (_unused, frame) => ({
		playing,
		position_ms: fromMs + frame * sampleMs
	}));
}

//-----------------------------------------------------------------------------
// the verdict it exists to produce
//-----------------------------------------------------------------------------

test('the stall window is a declared constant', () => {
	const mod = _stall();
	assert.equal(
		typeof mod.PRESENTATION_STALL_MS,
		'number',
		'if the window is not declared then broken - the threshold that decides "frozen" ' +
			'cannot live only in a test, or the app and its guard disagree about what a ' +
			'freeze is'
	);
	assert.ok(
		mod.PRESENTATION_STALL_MS > 100,
		'if the window is at or under a few frames then broken - one dropped frame on a ' +
			'loaded machine would fire the alarm continuously'
	);
});

test('a position that stops moving while playing emits presentation-stalled', () => {
	const mod = _stall();
	const { verdicts } = runSamples(frozenFor(mod.PRESENTATION_STALL_MS + 300));
	assert.equal(
		verdicts.length,
		1,
		`if a playing deck whose position_ms never changes emits ${verdicts.length} ` +
			'verdict(s) instead of 1 then broken - this is the Wed 2 Sep 2026 13:22-13:42 ' +
			'CEST failure exactly, and the app produced nothing at all'
	);
	assert.equal(verdicts[0].verdict, 'presentation-stalled');
	assert.ok(
		verdicts[0].tMs >= mod.PRESENTATION_STALL_MS,
		'if the verdict fires before the window elapses then broken - a brief hitch during a ' +
			'load or a seek would alarm on every track change'
	);
});

test('a stall shorter than the window is not a verdict', () => {
	const mod = _stall();
	const { verdicts } = runSamples(frozenFor(mod.PRESENTATION_STALL_MS - 200));
	assert.equal(
		verdicts.length,
		0,
		'if a sub-window freeze emits then broken - a single long paint or a GC pause would ' +
			'be reported as an incident'
	);
});

//-----------------------------------------------------------------------------
// the two ways it must NOT fire
//-----------------------------------------------------------------------------

test('a paused deck is never a verdict, however long it sits still', () => {
	const mod = _stall();
	const { verdicts } = runSamples(
		frozenFor(mod.PRESENTATION_STALL_MS * 20, { playing: false })
	);
	assert.equal(
		verdicts.length,
		0,
		'if a paused deck emits presentation-stalled then broken - a stopped playhead not ' +
			'moving is the definition of working correctly, and an alarm that fires at rest ' +
			'is an alarm the operator mutes before the set starts'
	);
});

test('a position that keeps moving is never a verdict', () => {
	const mod = _stall();
	const { verdicts } = runSamples(movingFor(mod.PRESENTATION_STALL_MS * 20));
	assert.equal(
		verdicts.length,
		0,
		'if a moving playhead emits presentation-stalled then broken - the watchdog would ' +
			'fire straight through a healthy set'
	);
});

test('the position moving again resets the stall clock', () => {
	const mod = _stall();
	const nearlyThere = frozenFor(mod.PRESENTATION_STALL_MS - 200);
	const { verdicts } = runSamples([
		...nearlyThere,
		{ playing: true, position_ms: 99_999 },
		...frozenFor(mod.PRESENTATION_STALL_MS - 200, { positionMs: 99_999 })
	]);
	assert.equal(
		verdicts.length,
		0,
		'if a single moving frame does not reset the clock then broken - two unrelated ' +
			'hitches would add up into a freeze that never happened'
	);
});

//-----------------------------------------------------------------------------
// usable, not merely correct
//-----------------------------------------------------------------------------

test('one freeze is one verdict, not one per frame', () => {
	const mod = _stall();
	const { verdicts } = runSamples(frozenFor(mod.PRESENTATION_STALL_MS * 30));
	assert.equal(
		verdicts.length,
		1,
		`if a single freeze emits ${verdicts.length} verdicts then broken - at 60Hz a ` +
			'twenty-minute freeze would be 72,000 toasts and 72,000 escalated perf events, ' +
			'on the same main thread as the audio this is meant to protect'
	);
});

test('a second freeze after recovery is a second verdict', () => {
	const mod = _stall();
	const freeze = frozenFor(mod.PRESENTATION_STALL_MS + 300);
	const moving = movingFor(300, { fromMs: 50_000 });
	const { verdicts } = runSamples([
		...freeze,
		...moving,
		...frozenFor(mod.PRESENTATION_STALL_MS + 300, { positionMs: 60_000 })
	]);
	assert.equal(
		verdicts.length,
		2,
		'if the second freeze is swallowed then broken - the watchdog would go permanently ' +
			'quiet after the first incident of a session'
	);
});

//-----------------------------------------------------------------------------
// house rules
//-----------------------------------------------------------------------------

test('the fold is pure: the state handed in is never mutated', () => {
	const mod = _stall();
	const first = mod.foldPresentationSample(undefined, {
		playing: true,
		position_ms: 1000,
		tMs: 0
	});
	const snapshot = JSON.stringify(first);
	const second = mod.foldPresentationSample(first, {
		playing: true,
		position_ms: 1000,
		tMs: 16.67
	});
	assert.equal(
		JSON.stringify(first),
		snapshot,
		'if the fold mutates its input then broken - a caller keeping the previous state to ' +
			'compare against would silently be comparing a value with itself'
	);
	assert.notEqual(first, second);
});

test('a position that is not a number is refused, not read as frozen', () => {
	const mod = _stall();
	for (const positionMs of [Number.NaN, undefined, Number.POSITIVE_INFINITY]) {
		assert.throws(
			() =>
				mod.foldPresentationSample(undefined, { playing: true, position_ms: positionMs, tMs: 0 }),
			RangeError,
			`if position_ms ${String(positionMs)} is accepted then broken - an unreadable ` +
				'position would compare equal to the last one forever and raise a freeze alarm ' +
				'through perfectly healthy playback'
		);
	}
});

test('time going backwards is refused rather than folded', () => {
	const mod = _stall();
	const first = mod.foldPresentationSample(undefined, {
		playing: true,
		position_ms: 1000,
		tMs: 1000
	});
	assert.throws(
		() => mod.foldPresentationSample(first, { playing: true, position_ms: 1000, tMs: 900 }),
		RangeError,
		'if a sample timestamped before the previous one is folded then broken - the elapsed ' +
			'arithmetic goes negative and the window never elapses'
	);
});
