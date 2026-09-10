/**
 * Two master-bus watchdogs, wired: read the meter, fold the samples, report
 * the edge. Both live here rather than in `audio-engine.svelte.ts`, which
 * sits near the ratchet's `file_size.max_frontend` cap, under convention D5.
 *
 * SILENCE (`noteMasterSilence`, split from the pure `silence-watchdog.ts`)
 * owns the three things that pure fold deliberately does not: the scratch
 * buffer, the RMS arithmetic, and the side effects.
 *
 * MEASURED AT THE MASTER BUS, not per deck. A per-deck analyser cannot see a
 * master gain at zero, a crossfader assigned away, or a graph that has quietly
 * stopped passing signal - all of which are "playing but silent" to the person
 * in the room. The tap sits on `_masterGain`, BEFORE the headless-test mute
 * node, so `?muted=1` does not read as a dropout.
 *
 * DEVICE LIVENESS (`_noteOutputDeviceLiveness`, issue #1642, pure fold in
 * `output-device-watchdog.ts`) is a SEPARATE claim, run as the last step of
 * every `noteMasterSilence` call so it shares one reading of the master bus
 * rather than sampling twice. It fires only when the graph is producing
 * signal (silence's own claim is false) AND the device's HAL clock has
 * stalled, so it is structurally unable to fire on the same sample silence
 * does.
 */

import { recordPerfEvent } from '$lib/rb/perf-event-log';
import {
	SILENT_WHILE_PLAYING_MS,
	foldSilenceSample,
	type SilenceState
} from '$lib/rb/silence-watchdog';
import { foldDeviceLivenessSample, type DeviceLivenessState } from '$lib/rb/output-device-watchdog';
import { isAnyPresentationClockStalled } from '$lib/rb/presentation-clock-report';
import { audioOutputHealth } from '$lib/rb/audio-output-health.svelte';
import { pushToast } from '$lib/stores.svelte';

let _state: SilenceState | undefined;
let _deviceLivenessState: DeviceLivenessState | undefined;
let _scratch: Float32Array | null = null;
let _lastMasterRms: number | null = null;

/** Instantaneous RMS 0..1 of whatever is leaving the master gain. */
function _masterRms(analyser: AnalyserNode): number {
	if (_scratch === null || _scratch.length !== analyser.fftSize) {
		_scratch = new Float32Array(new ArrayBuffer(analyser.fftSize * 4));
	}
	const buf = _scratch as Float32Array<ArrayBuffer>;
	analyser.getFloatTimeDomainData(buf);
	let sum = 0;
	for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
	const rms = Math.sqrt(sum / buf.length);
	return Number.isFinite(rms) ? rms : 0;
}

/**
 * Sample the master bus once and report a dropout on the sample that crosses
 * the window.
 *
 * A missing analyser is treated as "no reading", which resets the run rather
 * than counting as silence: a graph with no meter is not evidence of a dropout.
 *
 * `externalRouteAnalyser` is the `_externalMerger` tap, non-null only in
 * `?extroute=` sessions: a routed fader bypasses `_masterGain` entirely, so
 * `silent-while-playing` (which must stay reading `_masterGain` only, per its
 * own contract) would otherwise be the sole consumer able to see this signal
 * at all. Device liveness below reads both taps because it needs "is ANY
 * output path carrying signal", not "is the internal master bus".
 */
export function noteMasterSilence(
	analyser: AnalyserNode | null,
	externalRouteAnalyser: AnalyserNode | null,
	anyDeckPlaying: boolean,
	tMs: number
): void {
	const playing = anyDeckPlaying && analyser !== null;
	const masterRms = analyser === null ? 1 : _masterRms(analyser);
	_lastMasterRms = masterRms;
	_state = foldSilenceSample(_state, { playing, masterRms, tMs });
	if (_state.verdict === 'silent-while-playing') {
		recordPerfEvent(
			'silent-while-playing',
			`a deck has reported playing for ${SILENT_WHILE_PLAYING_MS}ms with nothing leaving ` +
				'the master bus - the transport is running and the room is hearing silence',
			null,
			'error'
		);
		pushToast('A deck says it is playing but no audio is leaving the mixer', 'error');
	}
	const routedRms = externalRouteAnalyser === null ? 0 : _masterRms(externalRouteAnalyser);
	_noteOutputDeviceLiveness(playing, Math.max(masterRms, routedRms), tMs);
}

/**
 * The room hearing nothing despite a live mixer (issue #1642): the device's
 * HAL clock stalled while the graph kept producing signal on ANY output
 * path - the internal master bus or, in `?extroute=` sessions, the routed
 * fader/merger path `_masterGain` never sees (P1 review thread on PR #1693).
 *
 * The clock stall alone cannot tell a dead device from the Wed 2 Sep 2026
 * CoreAudio timestamp glitch (device alive, rendering); `outputLatencyDead`
 * reads `audio-output-liveness.ts`'s own debounced verdict as the
 * corroborator that makes the two distinguishable (P1 review thread on PR
 * #1693, see `output-device-watchdog.ts`'s docstring).
 */
function _noteOutputDeviceLiveness(playing: boolean, masterRms: number, tMs: number): void {
	const outputLatencyDead =
		audioOutputHealth.snapshot?.verdict === 'dead' || audioOutputHealth.snapshot?.verdict === 'dead-escalated';
	_deviceLivenessState = foldDeviceLivenessSample(_deviceLivenessState, {
		playing,
		masterRms,
		deviceClockStalled: isAnyPresentationClockStalled(),
		outputLatencyDead,
		tMs
	});
	if (_deviceLivenessState.verdict !== 'device-unreachable') return;
	recordPerfEvent(
		'output-device-unreachable',
		'a deck is playing and the master bus is producing signal, but the output device\'s HAL ' +
			'position has stopped advancing: the graph is fine and the ROOM is hearing nothing - ' +
			'distinct from the mixer being quiet',
		null,
		'error'
	);
	pushToast('NO AUDIO REACHING THE ROOM: the output device appears gone, not the mixer', 'error');
}

/** Drop both runs across a graph rebuild, so a teardown is not a dropout. */
export function resetMasterSilenceWatch(): void {
	_state = undefined;
	_deviceLivenessState = undefined;
	_lastMasterRms = null;
}

/** Real master-bus reading and silence-watchdog verdict for the agent UI mirror. */
export function masterSilenceState(): { rms: number | null; verdict: SilenceState['verdict'] } {
	return { rms: _lastMasterRms, verdict: _state?.verdict ?? 'ok' };
}

/**
 * Device-liveness watchdog verdict for the agent UI mirror. Reads `live`, not
 * `verdict`: the mirror is polled once a second and must see the CURRENT
 * state of an ongoing outage, not just its single edge-triggered sample.
 */
export function outputDeviceLivenessState(): { verdict: DeviceLivenessState['live'] } {
	return { verdict: _deviceLivenessState?.live ?? 'ok' };
}
