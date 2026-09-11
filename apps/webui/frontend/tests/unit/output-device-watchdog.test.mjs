import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Issue #1642: no shipped instrument can tell "the mixer is quiet" apart from
 * "the room is hearing nothing". `master-silence-report.ts` and `meter-tap.ts`
 * both tap `_masterGain`, upstream of the output device, so a device-level
 * failure (the graph still producing signal, the bound device gone) reads as
 * healthy the whole time the room hears nothing.
 *
 * THE GATE IS `outputLatencyDead` ALONE - `audio-output-liveness.ts`'s own
 * debounced verdict that `AudioContext.outputLatency` read 0 for
 * `LIVENESS_DEAD_POLLS` consecutive polls while a deck played - corroborated
 * only by the master bus still producing signal. An earlier revision of this
 * module also required a presentation-clock stall (`getOutputTimestamp()`'s
 * `contextTime` frozen); two rounds of review found that requirement wrong in
 * both directions (P1 review threads on PR #1693): filtering the stall to
 * exclude the render-thread sample-clock fallback discarded exactly the
 * stalls a dead device produces, and even an unfiltered stall requirement
 * missed the real Wed 2 Sep 2026 incident outright - `audio-output-liveness.ts`'s
 * docstring records that outage as `outputLatency === 0` with the HAL
 * position still ADVANCING. A signal absent from the one production incident
 * this module exists to catch cannot be a required co-signal, so the fold no
 * longer takes one.
 *
 * DEVICE-AGNOSTIC BY CONSTRUCTION, like its sibling: the input is
 * `{playing, masterRms, outputLatencyDead, tMs}` - "it claims to be playing,
 * a shipped signal says the device itself is gone, and the mixer is not the
 * reason".
 *
 * Regression lines:
 * - if outputLatencyDead with signal present emits no verdict then the app
 *   still cannot tell a dead device from a healthy one (the #1642 gap, and
 *   the real Wed 2 Sep 2026 incident specifically)
 * - if signal present with outputLatencyDead FALSE emits a verdict then
 *   ordinary playback alarms constantly
 * - if outputLatencyDead with NO signal present emits a verdict then it
 *   duplicates silent-while-playing instead of staying a distinct claim
 * - if the verdict re-fires per sample then one outage is a toast storm
 */

const WATCHDOG_MODULE = 'src/lib/rb/output-device-watchdog.ts';
const SILENCE_MODULE = 'src/lib/rb/silence-watchdog.ts';

let watchdog = null;
let floor = null;
let loadError = null;

before(async () => {
	try {
		watchdog = await loadTypeScriptModule(WATCHDOG_MODULE);
		// The floor is the sibling module's constant, imported the same way the
		// TS source does (`$lib/rb/silence-watchdog`), not re-exported here: one
		// source of truth for "what counts as signal".
		floor = (await loadTypeScriptModule(SILENCE_MODULE)).SILENCE_RMS_FLOOR;
	} catch (error) {
		loadError = error;
	}
});

function _watchdog() {
	assert.equal(
		loadError,
		null,
		`if ${WATCHDOG_MODULE} does not exist then broken - nothing composes the debounced ` +
			'outputLatency-dead verdict with the master-bus reading, so a dead output device reads ' +
			`as a healthy mixer for as long as the outage lasts (bundler said: ${loadError === null ? '' : String(loadError)})`
	);
	return { foldDeviceLivenessSample: watchdog.foldDeviceLivenessSample, SILENCE_RMS_FLOOR: floor };
}

function runSamples(samples, sampleMs = 100) {
	const mod = _watchdog();
	const verdicts = [];
	let state;
	let tMs = 0;
	for (const sample of samples) {
		state = mod.foldDeviceLivenessSample(state, { ...sample, tMs });
		if (state.verdict !== 'ok') verdicts.push({ tMs, verdict: state.verdict });
		tMs += sampleMs;
	}
	return { verdicts, state };
}

function heldFor(ms, sample, sampleMs = 100) {
	return Array.from({ length: Math.ceil(ms / sampleMs) + 1 }, () => sample);
}

//-----------------------------------------------------------------------------
// the verdict it exists to produce
//-----------------------------------------------------------------------------

test('outputLatencyDead with signal present emits device-unreachable (the real Wed 2 Sep 2026 incident)', () => {
	const mod = _watchdog();
	const { verdicts } = runSamples(
		heldFor(500, {
			playing: true,
			masterRms: mod.SILENCE_RMS_FLOOR * 100,
			outputLatencyDead: true
		})
	);
	assert.equal(
		verdicts.length,
		1,
		'if a playing deck with signal above the floor and a debounced outputLatency-dead verdict ' +
			`emits ${verdicts.length} verdict(s) instead of 1 then broken - this is the #1642 gap: a ` +
			'graph producing signal into a dead device. The real Wed 2 Sep 2026 outage had the HAL ' +
			'clock still advancing while outputLatency read dead, so this must fire on outputLatencyDead ' +
			'alone, not on a presentation-clock stall too.'
	);
	assert.equal(verdicts[0].verdict, 'device-unreachable');
});

//-----------------------------------------------------------------------------
// the ways it must NOT fire
//-----------------------------------------------------------------------------

test('signal present but outputLatencyDead is false is never a verdict', () => {
	const mod = _watchdog();
	const { verdicts } = runSamples(
		heldFor(2_000, {
			playing: true,
			masterRms: mod.SILENCE_RMS_FLOOR * 100,
			outputLatencyDead: false
		})
	);
	assert.equal(
		verdicts.length,
		0,
		'if ordinary playback (signal present, outputLatency reporting its real nonzero figure) ' +
			'emits a verdict then broken - every healthy set would alarm'
	);
});

test('outputLatencyDead with the mixer ALSO silent is never a verdict', () => {
	const mod = _watchdog();
	const { verdicts } = runSamples(heldFor(2_000, { playing: true, masterRms: 0, outputLatencyDead: true }));
	assert.equal(
		verdicts.length,
		0,
		'if outputLatencyDead still fires while masterRms is 0 then broken - that case is already ' +
			"silent-while-playing's claim, and firing both blurs the two claims #1642 exists to " +
			'keep apart'
	);
});

test('outputLatencyDead with nothing playing is never a verdict', () => {
	const mod = _watchdog();
	const { verdicts } = runSamples(
		heldFor(2_000, {
			playing: false,
			masterRms: mod.SILENCE_RMS_FLOOR * 100,
			outputLatencyDead: true
		})
	);
	assert.equal(
		verdicts.length,
		0,
		'if a stopped deck emits device-unreachable then broken - a device that legitimately ' +
			'idles between tracks is not an outage'
	);
});

//-----------------------------------------------------------------------------
// the behaviours that decide whether it is usable rather than merely correct
//-----------------------------------------------------------------------------

test('recovery resets the report: a second outage is a second verdict', () => {
	const mod = _watchdog();
	const outage = heldFor(500, { playing: true, masterRms: mod.SILENCE_RMS_FLOOR * 100, outputLatencyDead: true });
	const recovered = heldFor(500, {
		playing: true,
		masterRms: mod.SILENCE_RMS_FLOOR * 100,
		outputLatencyDead: false
	});
	const { verdicts } = runSamples([...outage, ...recovered, ...outage]);
	assert.equal(
		verdicts.length,
		2,
		'if the second outage is swallowed then broken - the watchdog would go quiet ' +
			'permanently after the first incident of a session'
	);
});

test('one outage is one verdict, not one per sample', () => {
	const mod = _watchdog();
	const { verdicts } = runSamples(
		heldFor(5_000, { playing: true, masterRms: mod.SILENCE_RMS_FLOOR * 100, outputLatencyDead: true })
	);
	assert.equal(
		verdicts.length,
		1,
		`if a single outage emits ${verdicts.length} verdicts then broken - a per-sample verdict ` +
			'is a toast storm on top of an outage'
	);
});

test('live stays device-unreachable for the whole outage, not just the crossing sample (P1, PR #1693)', () => {
	const mod = _watchdog();
	let state;
	let tMs = 0;
	const liveReadings = [];
	for (const sample of heldFor(500, {
		playing: true,
		masterRms: mod.SILENCE_RMS_FLOOR * 100,
		outputLatencyDead: true
	})) {
		state = mod.foldDeviceLivenessSample(state, { ...sample, tMs });
		liveReadings.push(state.live);
		tMs += 100;
	}
	assert.ok(
		liveReadings.every((live) => live === 'device-unreachable'),
		'if `live` drops back to ok after the first sample of an ongoing outage then broken - a ' +
			'once-a-second UI mirror poll (buildUiMirror) reads `live` on whatever sample it lands ' +
			`on, and would report no ongoing problem for the rest of the outage, got: ${liveReadings.join(',')}`
	);
});

test('a momentary quiet frame mid-outage does not re-arm the report (P2, PR #1693)', () => {
	const mod = _watchdog();
	const deadLoud = { playing: true, masterRms: mod.SILENCE_RMS_FLOOR * 100, outputLatencyDead: true };
	const deadQuiet = { playing: true, masterRms: 0, outputLatencyDead: true };
	const { verdicts, state } = runSamples([deadLoud, deadLoud, deadQuiet, deadLoud, deadLoud]);
	assert.equal(
		verdicts.length,
		1,
		'if a single quiet frame in the middle of a continuous outage produces a second ' +
			'device-unreachable verdict then broken - a quiet break in the track would toast-storm ' +
			'one uninterrupted outage'
	);
	assert.equal(
		state.live,
		'device-unreachable',
		'the outage is still ongoing (outputLatency still dead, deck still playing) after the quiet ' +
			'frame, so `live` must still say so'
	);
});

//-----------------------------------------------------------------------------
// house rules: pure, and fail-fast on nonsense
//-----------------------------------------------------------------------------

test('the fold is pure: the state handed in is never mutated', () => {
	const mod = _watchdog();
	const first = mod.foldDeviceLivenessSample(undefined, {
		playing: true,
		masterRms: mod.SILENCE_RMS_FLOOR * 100,
		outputLatencyDead: true,
		tMs: 0
	});
	const snapshot = JSON.stringify(first);
	const second = mod.foldDeviceLivenessSample(first, {
		playing: true,
		masterRms: mod.SILENCE_RMS_FLOOR * 100,
		outputLatencyDead: true,
		tMs: 100
	});
	assert.equal(
		JSON.stringify(first),
		snapshot,
		'if the fold mutates its input then broken - a caller keeping the previous state for ' +
			'comparison would silently be comparing a value against itself'
	);
	assert.notEqual(first, second);
});

test('a meter reading that is not a number is refused, not treated as silence', () => {
	const mod = _watchdog();
	for (const masterRms of [Number.NaN, -1, undefined]) {
		assert.throws(
			() =>
				mod.foldDeviceLivenessSample(undefined, {
					playing: true,
					masterRms,
					outputLatencyDead: true,
					tMs: 0
				}),
			RangeError,
			`if masterRms ${String(masterRms)} is accepted then broken - a broken meter would be ` +
				'read as evidence either way'
		);
	}
});

test('time going backwards is refused rather than folded', () => {
	const mod = _watchdog();
	const first = mod.foldDeviceLivenessSample(undefined, {
		playing: true,
		masterRms: mod.SILENCE_RMS_FLOOR * 100,
		outputLatencyDead: true,
		tMs: 1000
	});
	assert.throws(
		() =>
			mod.foldDeviceLivenessSample(first, {
				playing: true,
				masterRms: mod.SILENCE_RMS_FLOOR * 100,
				outputLatencyDead: true,
				tMs: 900
			}),
		RangeError,
		'if a sample timestamped earlier than the last one is folded then broken - the ordering ' +
			'this edge-triggered verdict relies on would be violated'
	);
});
