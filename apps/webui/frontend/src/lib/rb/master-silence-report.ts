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
 * signal (silence's own claim is false) AND `audio-output-liveness.ts`'s
 * debounced verdict says `outputLatency` has read dead, so it is
 * structurally unable to fire on the same sample silence does.
 */

import { recordPerfEvent, readPerfEvents } from '$lib/rb/perf-event-log';
import {
	SILENT_WHILE_PLAYING_MS,
	foldSilenceSample,
	type SilenceState,
	type SilenceVerdict
} from '$lib/rb/silence-watchdog';
import { foldDeviceLivenessSample, type DeviceLivenessState } from '$lib/rb/output-device-watchdog';
import { audioOutputHealth } from '$lib/rb/audio-output-health.svelte';
import {
	diagnoseSilenceCause,
	formatAudioCutToast,
	lastPerfError,
	planSilenceDropout,
	type SilenceDropoutDeckSnap,
	type SilenceDropoutPlan
} from '$lib/rb/silence-dropout';
import { pushToast } from '$lib/stores.svelte';
import type { DeckId } from '$lib/rb/deck-slots';

export interface SilenceDropoutContext {
	decks: readonly SilenceDropoutDeckSnap[];
	autoplay_enabled: boolean;
	has_playable_next: boolean;
	xruns: number;
	xruns_at_previous: number;
}

let _state: SilenceState | undefined;
let _deviceLivenessState: DeviceLivenessState | undefined;
let _scratch: Float32Array | null = null;
let _lastMasterRms: number | null = null;
/** Wall-clock ms when `_lastMasterRms` was written; null when never written. */
let _lastMasterRmsAtMs: number | null = null;
let _xrunsAtPreviousSample = 0;
let _dropoutHandler: ((plan: SilenceDropoutPlan) => void) | null = null;
let _readDropoutContext: (() => SilenceDropoutContext) | null = null;

export function setSilenceDropoutHandler(handler: ((plan: SilenceDropoutPlan) => void) | null): void {
	_dropoutHandler = handler;
}

export function setSilenceDropoutContextReader(reader: (() => SilenceDropoutContext) | null): void {
	_readDropoutContext = reader;
}

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

function _anyDeckAudible(ctx: SilenceDropoutContext | null): boolean {
	if (ctx === null) return false;
	return ctx.decks.some((deck) => deck.audible);
}

function _reportSilenceDropout(kind: 'silent-while-playing' | 'output-device-unreachable'): void {
	const ctx = _readDropoutContext?.() ?? null;
	const events = readPerfEvents();
	const xruns = ctx?.xruns ?? 0;
	const cause = diagnoseSilenceCause({
		decks: ctx?.decks ?? [],
		events,
		xruns,
		xruns_at_previous: ctx?.xruns_at_previous ?? _xrunsAtPreviousSample
	});
	const last = lastPerfError(events);
	const toast = formatAudioCutToast({
		decks: ctx?.decks ?? [],
		cause,
		last_error: last
	});
	if (kind === 'silent-while-playing') {
		const plan =
			ctx === null
				? {
						stop_decks: [] as DeckId[],
						toast,
						perf_kind: 'silent-while-playing' as const,
						cause,
						cause_message: `silent while claimed live cause=${cause} ${last === null ? 'last=none' : `last=${last.kind}: ${last.message}`}`,
						autoplay_recover: false
					}
				: planSilenceDropout({
						decks: ctx.decks,
						events,
						xruns: ctx.xruns,
						xruns_at_previous: ctx.xruns_at_previous,
						autoplay_enabled: ctx.autoplay_enabled,
						has_playable_next: ctx.has_playable_next
					});
		recordPerfEvent(plan.perf_kind, plan.cause_message, plan.stop_decks[0] ?? null, 'error');
		pushToast(plan.toast, 'error');
		_dropoutHandler?.(plan);
		return;
	}
	recordPerfEvent(
		'output-device-unreachable',
		'a deck is playing and the master bus is producing signal, but the output device reads ' +
			'dead (outputLatency stuck at 0): the graph is fine and the ROOM is hearing nothing - ' +
			`cause=${cause} ${last === null ? 'last=none' : `last=${last.kind}: ${last.message}`}`,
		null,
		'error'
	);
	pushToast(toast, 'error');
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
	const ctx = _readDropoutContext?.() ?? null;
	const playing = anyDeckPlaying && analyser !== null;
	const masterRms = analyser === null ? NO_METER_RMS_SENTINEL : _masterRms(analyser);
	if (analyser === null) {
		_lastMasterRms = null;
		_lastMasterRmsAtMs = null;
	} else {
		_lastMasterRms = masterRms;
		_lastMasterRmsAtMs = Date.now();
	}
	_state = foldSilenceSample(_state, {
		playing,
		audible: _anyDeckAudible(ctx),
		masterRms,
		tMs
	});
	if (_state.verdict === 'silent-while-playing') {
		_reportSilenceDropout('silent-while-playing');
	}
	const routedRms = externalRouteAnalyser === null ? 0 : _masterRms(externalRouteAnalyser);
	_noteOutputDeviceLiveness(playing, Math.max(masterRms, routedRms), tMs);
	if (ctx !== null) _xrunsAtPreviousSample = ctx.xruns;
}

/**
 * The room hearing nothing despite a live mixer (issue #1642): the device
 * reads dead while the graph kept producing signal on ANY output path - the
 * internal master bus or, in `?extroute=` sessions, the routed fader/merger
 * path `_masterGain` never sees (P1 review thread on PR #1693).
 *
 * `outputLatencyDead` reads `audio-output-liveness.ts`'s own debounced
 * verdict; it is the sole device-loss signal, not a corroborator of a
 * presentation-clock stall - see `output-device-watchdog.ts`'s docstring for
 * why the stall was dropped as a required co-signal (it missed the real
 * Wed 2 Sep 2026 incident, where the HAL clock kept advancing).
 */
function _noteOutputDeviceLiveness(playing: boolean, masterRms: number, tMs: number): void {
	const browserVerdict = audioOutputHealth.snapshot?.browser?.verdict;
	const outputLatencyDead = browserVerdict === 'dead' || browserVerdict === 'dead-escalated';
	_deviceLivenessState = foldDeviceLivenessSample(_deviceLivenessState, {
		playing,
		masterRms,
		outputLatencyDead,
		tMs
	});
	if (_deviceLivenessState.verdict !== 'device-unreachable') return;
	_reportSilenceDropout('output-device-unreachable');
}

/** Drop both runs across a graph rebuild, so a teardown is not a dropout. */
export function resetMasterSilenceWatch(): void {
	_state = undefined;
	_deviceLivenessState = undefined;
	_lastMasterRms = null;
	_lastMasterRmsAtMs = null;
	_xrunsAtPreviousSample = 0;
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
/** Mixer is rendering loudly while the device output position is frozen (issue #2155). */
export const RENDERING_RMS_FLOOR = 0.05;

export function masterSilenceState(): {
	rms: number | null;
	verdict: SilenceVerdict;
	at_ms: number | null;
} {
	const outputStalled = audioOutputHealth.snapshot?.browser?.verdict === 'stalled';
	if (
		outputStalled &&
		_lastMasterRms !== null &&
		_lastMasterRms > RENDERING_RMS_FLOOR &&
		(_state?.verdict ?? 'ok') === 'ok'
	) {
		return { rms: _lastMasterRms, verdict: 'output-stalled-while-rendering', at_ms: _lastMasterRmsAtMs };
	}
	return { rms: _lastMasterRms, verdict: _state?.verdict ?? 'ok', at_ms: _lastMasterRmsAtMs };
}

/**
 * Device-liveness watchdog verdict for the agent UI mirror. Reads `live`, not
 * `verdict`: the mirror is polled once a second and must see the CURRENT
 * state of an ongoing outage, not just its single edge-triggered sample.
 */
export function outputDeviceLivenessState(): { verdict: DeviceLivenessState['live'] } {
	return { verdict: _deviceLivenessState?.live ?? 'ok' };
}
