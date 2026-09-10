/**
 * The master-bus silence watchdog, wired: reads the meter, folds the samples,
 * reports the edge.
 *
 * Split from `silence-watchdog.ts` (which is the pure decision) and from the
 * engine (which sits near the ratchet's `file_size.max_frontend` cap) under
 * convention D5. It owns the three things the pure fold deliberately does not:
 * the scratch buffer, the RMS arithmetic, and the side effects.
 *
 * MEASURED AT THE MASTER BUS, not per deck. A per-deck analyser cannot see a
 * master gain at zero, a crossfader assigned away, or a graph that has quietly
 * stopped passing signal - all of which are "playing but silent" to the person
 * in the room. The tap sits on `_masterGain`, BEFORE the headless-test mute
 * node, so `?muted=1` does not read as a dropout.
 */

import { recordPerfEvent } from '$lib/rb/perf-event-log';
import {
	SILENT_WHILE_PLAYING_MS,
	foldSilenceSample,
	type SilenceState
} from '$lib/rb/silence-watchdog';
import { pushToast } from '$lib/stores.svelte';

let _state: SilenceState | undefined;
let _scratch: Float32Array | null = null;
let _lastMasterRms: number | null = null;
/** Wall-clock ms when `_lastMasterRms` was written; null when never written. */
let _lastMasterRmsAtMs: number | null = null;

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
 * The value handed to the fold when there is no meter to read.
 *
 * Full scale, so the fold sees "not silence" and resets the run: a graph with
 * no analyser is not evidence of a dropout. It is a CONTROL VALUE, never a
 * measurement, which is why `noteMasterSilence` refuses to publish it.
 */
const NO_METER_RMS_SENTINEL = 1;

/**
 * Sample the master bus once and report a dropout on the sample that crosses
 * the window.
 *
 * A missing analyser is treated as "no reading", which resets the run rather
 * than counting as silence: a graph with no meter is not evidence of a dropout.
 */
export function noteMasterSilence(
	analyser: AnalyserNode | null,
	anyDeckPlaying: boolean,
	tMs: number
): void {
	const playing = anyDeckPlaying && analyser !== null;
	const masterRms = analyser === null ? NO_METER_RMS_SENTINEL : _masterRms(analyser);
	// ONLY A REAL SAMPLE IS PUBLISHED (Codex, Thu 10 Sep 2026). Stamping the
	// no-meter sentinel made the mirror read `rms: 1, fresh: true` during graph
	// initialization and teardown - a synthetic full-scale value wearing the
	// costume of a fresh measurement, which is the exact misleading
	// healthy-signal diagnosis this whole change exists to remove. No analyser
	// means no reading, and the mirror is told so.
	if (analyser === null) {
		_lastMasterRms = null;
		_lastMasterRmsAtMs = null;
	} else {
		_lastMasterRms = masterRms;
		_lastMasterRmsAtMs = Date.now();
	}
	_state = foldSilenceSample(_state, { playing, masterRms, tMs });
	if (_state.verdict !== 'silent-while-playing') return;
	recordPerfEvent(
		'silent-while-playing',
		`a deck has reported playing for ${SILENT_WHILE_PLAYING_MS}ms with nothing leaving ` +
			'the master bus - the transport is running and the room is hearing silence',
		null,
		'error'
	);
	pushToast('A deck says it is playing but no audio is leaving the mixer', 'error');
}

/** Drop the run across a graph rebuild, so a teardown is not a dropout. */
export function resetMasterSilenceWatch(): void {
	_state = undefined;
	_lastMasterRms = null;
	_lastMasterRmsAtMs = null;
}

/**
 * Real master-bus reading and watchdog verdict for the agent UI mirror.
 *
 * `at_ms` is load bearing. The RAF loop that calls `noteMasterSilence` stops
 * when nothing is transporting (`audio-engine.svelte.ts:1857`) while the mirror
 * keeps re-publishing the last value every second, so without a stamp a reader
 * cannot tell a live 0.24 from one frozen at the moment audio died. That
 * ambiguity cost most of a morning on Thu 10 Sep 2026. Wall clock rather than
 * the fold's `tMs`, so the mirror can compute an age against `Date.now()`
 * without knowing which clock base the caller used.
 */
export function masterSilenceState(): {
	rms: number | null;
	verdict: SilenceState['verdict'];
	at_ms: number | null;
} {
	return { rms: _lastMasterRms, verdict: _state?.verdict ?? 'ok', at_ms: _lastMasterRmsAtMs };
}
