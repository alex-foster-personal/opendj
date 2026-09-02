import assert from 'node:assert/strict';
import { test } from 'node:test';

import { engineBlockAfter, readFrontendSource } from './engine-source.mjs';

/**
 * waveform-freezes-on-stale-output-timestamp, mechanism 2: one throw must not
 * be the end of the presentation clock.
 *
 * THIS FILE IS SOURCE-LEVEL, DELIBERATELY, AND THAT IS A WEAKNESS WORTH
 * STATING PLAINLY.
 *
 * `_tick`, `_ensureRaf`, `_readOutputTimestamp` and `_publishPresentedTransport`
 * are all module-private in `audio-engine.svelte.ts` and no test anywhere
 * executes them. Driving them behaviourally would mean exporting a test seam
 * from the engine, and this branch is forbidden from editing that file - and
 * rightly so, since a seam added to make a red test green is a change to the
 * thing under test. `tests/unit/transport-visual-feedback.test.mjs` and
 * `autoplay-background-tab-clock.test.mjs` use the same escape hatch for the
 * same reason. So these are drift guards on the shape of the code, not proofs
 * about its behaviour, and they should be REPLACED by behavioural tests as
 * soon as the fix makes the loop drivable.
 *
 * The failure they guard against:
 *
 *   _tick() nulls _rafId at :2120, BEFORE doing any work, and has no catch.
 *   Any throw between :2122 and :2132 therefore leaves no pending frame and no
 *   handler. The only thing that re-arms the loop is _ensureRaf() from
 *   _scheduleDeck (:1672), and free-running playback never calls _scheduleDeck.
 *   So one throw kills the waveform permanently while audio plays on, with no
 *   recovery short of a transport action by the operator.
 *
 * The likeliest thrower is `presentation.ts:216-221` rejecting a non-finite
 * `performanceTime` - a value WebKit extrapolates, and which the module's own
 * comment (`presentation.ts:261`) calls "correlation/diagnostic data, never
 * transport authority". `presentation-clock-stall.test.mjs` asserts that
 * throw away at the source; this file asserts that even if something else
 * throws, the loop survives it.
 *
 * Regression lines:
 * - if _tick can throw without re-arming then a single bad frame is terminal
 *   and the only symptom is a frozen waveform over healthy audio
 * - if _scheduleDeck is the only re-arm then recovery requires the operator to
 *   touch the transport, which is exactly what they will not do when the
 *   playhead looks stuck
 * - if a throw inside the tick leaves no perf event then the incident is
 *   unreconstructable afterwards, which is how 13:22-13:42 was spent guessing
 */

const TICK_ANCHOR = 'function _tick(): void {';
const ENSURE_RAF_ANCHOR = 'function _ensureRaf(): void {';

//-----------------------------------------------------------------------------
// the loop must survive its own body throwing
//-----------------------------------------------------------------------------

test('SOURCE: the tick body is guarded, so one bad frame is not terminal', () => {
	const tick = engineBlockAfter(TICK_ANCHOR);
	assert.ok(
		/\btry\b/.test(tick) && /\bcatch\b/.test(tick),
		'if _tick runs its body unguarded then broken - it nulls _rafId before doing any ' +
			'work, so any throw leaves no pending frame, no handler, and a permanently dead ' +
			'presentation clock while audio keeps playing'
	);
});

test('SOURCE: a failing frame still re-arms the next one', () => {
	const tick = engineBlockAfter(TICK_ANCHOR);
	// The re-arm must be reachable on the failure path. A requestAnimationFrame
	// that only appears after the work is a re-arm the throw jumps over.
	const guardIndex = tick.search(/\bcatch\b|\bfinally\b/);
	assert.ok(
		guardIndex !== -1,
		'if there is no catch/finally at all then broken - see the previous test'
	);
	assert.ok(
		/requestAnimationFrame|_ensureRaf/.test(tick.slice(guardIndex)),
		'if the failure path does not re-arm the loop then broken - the frame that threw is ' +
			'then the last frame that will ever run, and the waveform freezes over live audio ' +
			'with no way back except a transport action the operator has no reason to take'
	);
});

test('SOURCE: a throw inside the tick is recorded, not swallowed', () => {
	const tick = engineBlockAfter(TICK_ANCHOR);
	assert.ok(
		/notePresentationTickFailure|recordPerfEvent|reportClientError/.test(tick),
		'if the tick catches and says nothing then broken - a silently swallowed throw is ' +
			'strictly worse than the crash, because the waveform still freezes and now there ' +
			'is no evidence at all; 13:22-13:42 CEST Wed 2 Sep 2026 was reconstructed from ' +
			'CoreAudio logs precisely because the app recorded nothing'
	);
	// ...and the thing it calls must really record, at a severity that escalates.
	// Following the call one hop is what stops this passing on a no-op named
	// convincingly.
	const reporter = readFrontendSource('src/lib/rb/presentation-clock-report.ts');
	const body = reporter.slice(reporter.indexOf('export function notePresentationTickFailure'));
	assert.ok(
		body.includes("recordPerfEvent(") && body.includes("'error'"),
		'if notePresentationTickFailure does not record at error severity then broken - a ' +
			'warn-severity row never reaches /api/v1/client-errors and the incident stays ' +
			'inside the browser that suffered it'
	);
});

//-----------------------------------------------------------------------------
// the re-arm must not depend on the operator doing something
//-----------------------------------------------------------------------------

test('SOURCE: the loop is re-armed by something free-running playback reaches', () => {
	const engine = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const callSites = engine.split('_ensureRaf()').length - 1 - 1; // minus the definition
	assert.ok(
		callSites >= 2,
		`if _ensureRaf() has ${callSites} call site(s) then broken - today the only one is ` +
			'inside _scheduleDeck, and a deck playing straight through calls _scheduleDeck ' +
			'exactly never, so a loop that stops during free-running playback is never ' +
			'restarted by anything'
	);
});

test('SOURCE: _ensureRaf is still the single guarded entry point to the loop', () => {
	// A control on the fix rather than on the bug: whatever new re-arm lands, it
	// must go through the null check, or two frames end up scheduled per tick and
	// the presentation rate silently doubles.
	const ensure = engineBlockAfter(ENSURE_RAF_ANCHOR);
	assert.ok(
		ensure.includes('_rafId === null'),
		'if _ensureRaf stops guarding on a null _rafId then broken - concurrent re-arms ' +
			'would double the presentation publish rate and the audio-Hz meter with it'
	);
});

//-----------------------------------------------------------------------------
// the painters must not report health they cannot see
//-----------------------------------------------------------------------------

test('SOURCE: the frozen-playhead watchdog is wired, not merely written', () => {
	// WaveRow gates its loop on deck.playing, and audio-engine.svelte.ts:1657
	// sets st.playing = rt.desiredActive - desired INTENT, not presented truth.
	// So a dead presentation clock leaves WaveRow repainting an identical frame
	// at 60Hz with nothing comparing this frame's position against the last.
	//
	// Grepping for a word like "presentation" would pass on a COMMENT, so this
	// pins the one thing a comment cannot satisfy: a real import of the stall
	// module whose contract waveform-stall-watchdog.test.mjs defines.
	const consumers = [
		'src/lib/rb/audio-engine.svelte.ts',
		'src/lib/components/rb/wave/WaveRow.svelte',
		'src/lib/components/rb/deck/StripWaveform.svelte'
	];
	const wired = consumers.filter((path) =>
		readFrontendSource(path).includes('presentation-stall')
	);
	assert.ok(
		wired.length >= 1,
		'if nothing imports the presentation-stall watchdog then broken - a pure module ' +
			'nobody calls catches no incidents, and the painters would still repaint a ' +
			'pixel-identical frame forever and call that healthy, which is what they did for ' +
			`twenty minutes on Wed 2 Sep 2026 (checked: ${consumers.join(', ')})`
	);
});
