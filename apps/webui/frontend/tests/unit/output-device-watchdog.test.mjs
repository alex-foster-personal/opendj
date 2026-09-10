import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Issue #1642: no shipped instrument can tell "the mixer is quiet" apart from
 * "the room is hearing nothing". `master-silence-report.ts` and `meter-tap.ts`
 * both tap `_masterGain`, upstream of the output device, so a device-level
 * failure (the graph still producing signal, the bound device gone) reads as
 * healthy the whole time the room hears nothing. `audio-output-liveness.ts`'s
 * `outputLatency === 0` inference is a Chromium quirk, not a measurement.
 *
 * THIS MODULE COMPOSES AN EXISTING, PROVEN PROBE rather than adding a new one:
 * `presentation.ts`'s `observePresentedTransportTimeline` already detects
 * `getOutputTimestamp().contextTime` frozen while `performanceTime` advances,
 * proven against two real incidents (Wed 2 Sep 2026, Wed 9 Sep 2026 17:44Z).
 * The gap this module closes is that nothing gates that stall on whether the
 * mixer is STILL producing signal, so nothing can say "device gone, graph
 * fine" as a claim distinct from "the mixer is quiet".
 *
 * DEVICE-AGNOSTIC BY CONSTRUCTION, like its sibling: the input is
 * `{playing, masterRms, deviceClockStalled, outputLatencyDead, tMs}` - "it
 * claims to be playing, the device clock has stalled, a second shipped
 * signal corroborates the device itself is gone, and the mixer is not the
 * reason".
 *
 * `outputLatencyDead` exists because `deviceClockStalled` alone cannot tell
 * a dead device from the Wed 2 Sep 2026 CoreAudio timestamp glitch (device
 * alive, rendering, `contextTime` merely stale) - `presentation.ts` falls
 * back to the render-thread sample clock in BOTH cases, so `clock_source`
 * cannot discriminate them either (P1 review thread on PR #1693). The
 * corroborator is `audio-output-liveness.ts`'s own debounced verdict that
 * `outputLatency` read 0 for `LIVENESS_DEAD_POLLS` consecutive polls.
 *
 * Regression lines:
 * - if a stalled device clock with signal present AND outputLatencyDead
 *   emits no verdict then the app still cannot tell a dead device from a
 *   healthy one
 * - if a stalled device clock with signal present but outputLatencyDead
 *   FALSE emits a verdict then a benign clock glitch (device alive) falsely
 *   reports the room silent
 * - if a stalled device clock with NO signal present emits a verdict then it
 *   duplicates silent-while-playing instead of staying a distinct claim
 * - if an unstalled clock with signal present emits a verdict then ordinary
 *   playback alarms constantly
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
		`if ${WATCHDOG_MODULE} does not exist then broken - nothing composes the presentation ` +
			'clock stall with the master-bus reading, so a dead output device reads as a healthy ' +
			`mixer for as long as the outage lasts (bundler said: ${loadError === null ? '' : String(loadError)})`
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

test('a stalled device clock with signal present and outputLatencyDead emits device-unreachable', () => {
	const mod = _watchdog();
	const { verdicts } = runSamples(
		heldFor(500, {
			playing: true,
			masterRms: mod.SILENCE_RMS_FLOOR * 100,
			deviceClockStalled: true,
			outputLatencyDead: true
		})
	);
	assert.equal(
		verdicts.length,
		1,
		'if a playing deck with signal above the floor, a stalled device clock, and a debounced ' +
			`outputLatency-dead verdict emits ${verdicts.length} verdict(s) instead of 1 then broken - ` +
			'this is the #1642 gap: a graph producing signal into a dead device'
	);
	assert.equal(verdicts[0].verdict, 'device-unreachable');
});

//-----------------------------------------------------------------------------
// the ways it must NOT fire
//-----------------------------------------------------------------------------

test('a stalled device clock without outputLatencyDead is never a verdict (P1, PR #1693)', () => {
	const mod = _watchdog();
	const { verdicts } = runSamples(
		heldFor(2_000, {
			playing: true,
			masterRms: mod.SILENCE_RMS_FLOOR * 100,
			deviceClockStalled: true,
			outputLatencyDead: false
		})
	);
	assert.equal(
		verdicts.length,
		0,
		'if a clock stall alone (no outputLatency corroboration) fires then broken - this is the ' +
			'Wed 2 Sep 2026 CoreAudio timestamp glitch: contextTime froze while the device kept ' +
			'rendering, and clock_source cannot tell that apart from a dead device, so the caller ' +
			'must not either'
	);
});

test('a stalled device clock with the mixer ALSO silent is never a verdict', () => {
	const mod = _watchdog();
	const { verdicts } = runSamples(
		heldFor(2_000, { playing: true, masterRms: 0, deviceClockStalled: true, outputLatencyDead: true })
	);
	assert.equal(
		verdicts.length,
		0,
		'if a stalled clock still fires while masterRms is 0 then broken - that case is already ' +
			"silent-while-playing's claim, and firing both blurs the two claims #1642 exists to " +
			'keep apart'
	);
});

test('signal present but the device clock is NOT stalled is never a verdict', () => {
	const mod = _watchdog();
	const { verdicts } = runSamples(
		heldFor(2_000, {
			playing: true,
			masterRms: mod.SILENCE_RMS_FLOOR * 100,
			deviceClockStalled: false,
			outputLatencyDead: true
		})
	);
	assert.equal(
		verdicts.length,
		0,
		'if ordinary playback (signal present, device clock advancing normally) emits a verdict ' +
			'then broken - every healthy set would alarm'
	);
});

test('a stalled device clock with nothing playing is never a verdict', () => {
	const mod = _watchdog();
	const { verdicts } = runSamples(
		heldFor(2_000, {
			playing: false,
			masterRms: mod.SILENCE_RMS_FLOOR * 100,
			deviceClockStalled: true,
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
	const outage = heldFor(500, {
		playing: true,
		masterRms: mod.SILENCE_RMS_FLOOR * 100,
		deviceClockStalled: true,
		outputLatencyDead: true
	});
	const recovered = heldFor(500, {
		playing: true,
		masterRms: mod.SILENCE_RMS_FLOOR * 100,
		deviceClockStalled: false,
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
		heldFor(5_000, {
			playing: true,
			masterRms: mod.SILENCE_RMS_FLOOR * 100,
			deviceClockStalled: true,
			outputLatencyDead: true
		})
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
		deviceClockStalled: true,
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
	const stalledLoud = {
		playing: true,
		masterRms: mod.SILENCE_RMS_FLOOR * 100,
		deviceClockStalled: true,
		outputLatencyDead: true
	};
	const stalledQuiet = { playing: true, masterRms: 0, deviceClockStalled: true, outputLatencyDead: true };
	const { verdicts, state } = runSamples([stalledLoud, stalledLoud, stalledQuiet, stalledLoud, stalledLoud]);
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
		'the outage is still ongoing (clock still stalled, deck still playing) after the quiet ' +
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
		deviceClockStalled: true,
		outputLatencyDead: true,
		tMs: 0
	});
	const snapshot = JSON.stringify(first);
	const second = mod.foldDeviceLivenessSample(first, {
		playing: true,
		masterRms: mod.SILENCE_RMS_FLOOR * 100,
		deviceClockStalled: true,
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
					deviceClockStalled: true,
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
		deviceClockStalled: true,
		outputLatencyDead: true,
		tMs: 1000
	});
	assert.throws(
		() =>
			mod.foldDeviceLivenessSample(first, {
				playing: true,
				masterRms: mod.SILENCE_RMS_FLOOR * 100,
				deviceClockStalled: true,
				outputLatencyDead: true,
				tMs: 900
			}),
		RangeError,
		'if a sample timestamped earlier than the last one is folded then broken - the ordering ' +
			'this edge-triggered verdict relies on would be violated'
	);
});
