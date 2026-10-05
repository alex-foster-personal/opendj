/**
 * Q1 / S2: the operator's press, as a number the perf ring can carry.
 *
 * Extracted from `audio-engine.svelte.ts` under convention D5
 * (docs/perf/performance-register.md): the engine keeps the call sites, the
 * arithmetic and its refusal rule live here where a test can reach them.
 *
 * A press stamp is `event.timeStamp`, which is already on the
 * `performance.now()` epoch. Nothing in this module awaits, so a Class A
 * control never gains a wait by being measured.
 */

import { EQ_APPLY_KIND, eqApplyStages } from '$lib/player/eq-apply';
import { mixerApplyStages } from '$lib/player/mixer-apply';
import {
	latencyFloorLabels,
	type ScheduleRowKind,
	scheduleRowKind,
	unavailableLatencyTerms
} from '$lib/player/transport/press-audible';
import { claimArmedHotCuePress, claimLoadSpanningPress } from '$lib/rb/deck-slots';
import { recordPerfEvent, recordPerfTiming } from '$lib/rb/perf-event-log';

export { applyEqRamp } from '$lib/player/eq-apply';
export { FILTER_APPLY_KIND, FADER_APPLY_KIND, XFADER_APPLY_KIND, STEM_MUTE_APPLY_KIND, STEM_SOLO_APPLY_KIND } from '$lib/player/mixer-apply';

/**
 * A deck, exactly as the perf ring accepts one.
 *
 * Derived from `recordPerfEvent` rather than imported from
 * `$lib/rb/deck-slots` on purpose: an instrument whose only use for a deck is
 * to forward it to the ring has no business widening that module's importer
 * count.
 */
type PerfDeck = Parameters<typeof recordPerfEvent>[2];

/**
 * Turn an input stamp into the press-to-schedule delta, or nothing.
 *
 * The delta is the wait the operator actually felt before this schedule reached
 * the audio clock: the command scheduler's scope wait, `_resumeContext()`, and
 * the deck's own `rt.scheduleTail`.
 *
 * A stamp from a different epoch would produce a negative delta, and a logged
 * negative would UNDERSTATE the P0 budget it gates - the one direction a
 * latency instrument must never fail in. So an implausible reading is recorded
 * loudly and then dropped: the row loses its press stages rather than carrying
 * a lie, and the transport itself is never interrupted, because an instrument
 * may not break the thing it measures.
 */
export function measurePressToScheduleMs(
	pressT0Ms: number | undefined,
	deck: PerfDeck
): number | undefined {
	if (pressT0Ms === undefined) return undefined;
	const elapsedMs = performance.now() - pressT0Ms;
	if (!Number.isFinite(elapsedMs) || elapsedMs < 0) {
		recordPerfEvent(
			'press-stamp-implausible',
			`deck ${deck} press stamp ${pressT0Ms} yields ${elapsedMs}ms to the audio clock; ` +
				'the stamp is not on the performance.now() epoch, so press_to_schedule_ms and ' +
				'input_to_audible_ms are dropped from this row',
			deck
		);
		return undefined;
	}
	return elapsedMs;
}

/** LATENCY-01: log input stamp vs AudioContext.currentTime at a mixer apply. */
export function logMixerApply(
	kind: string,
	deck: PerfDeck,
	pressT0Ms: number | undefined,
	nowSec: number
): void {
	if (pressT0Ms === undefined) return;
	const pressToApplyMs = measurePressToScheduleMs(pressT0Ms, deck);
	if (pressToApplyMs === undefined) return;
	recordPerfTiming(kind, mixerApplyStages({ pressToApplyMs }), deck, {
		apply_context_time_s: String(nowSec)
	});
}

/** LATENCY-03: log input stamp vs AudioContext.currentTime at the EQ apply. */
export function logEqApply(
	deck: PerfDeck,
	pressT0Ms: number | undefined,
	nowSec: number,
	rampSec: number
): void {
	if (pressT0Ms === undefined) return;
	const pressToApplyMs = measurePressToScheduleMs(pressT0Ms, deck);
	if (pressToApplyMs === undefined) return;
	recordPerfTiming(
		EQ_APPLY_KIND,
		eqApplyStages({
			pressToApplyMs,
			rampStartOffsetMs: 0,
			rampDurationMs: rampSec * 1000
		}),
		deck,
		{ apply_context_time_s: String(nowSec) }
	);
}

/**
 * Everything the ring needs to file one schedule row: its kind and its
 * device-floor labels, DERIVED FROM THE ROW'S OWN STAGES.
 *
 * Taking the stages rather than re-reading the context is what makes the
 * labels honest. `scheduleOffsetStages` already omits `base_latency_ms` and
 * `output_latency_ms` when the device floor is not a real measurement, and
 * omits `press_to_schedule_ms` when no press is behind the schedule, so the
 * absences this reads are the SAME absences the row reports. A second read of
 * the context could disagree with the row beside it - the context can move
 * from `suspended` to `running` while the worklet acknowledgement is in
 * flight - and a row whose labels describe a later state than its own timing
 * is worse than one with no labels at all.
 *
 * `contextState` is the one fact the stages cannot carry, so it is passed and
 * must be read at the same point the stages were built.
 *
 * Composed HERE rather than at the call site because
 * `audio-engine.svelte.ts` sits at the quality ratchet's
 * `frontend.max_fan_out` ceiling (36 of 36 on main, Wed 9 Sep 2026) as well
 * as its `file_size.max_frontend` ceiling (4069 of 4069), so it can afford
 * neither a fourth import for these three functions nor the lines of
 * composition. This module is already one of its imports and is already the
 * press instrument.
 *
 * `pressT0Ms` rides along for two more lookups, `claimLoadSpanningPress` and
 * `claimArmedHotCuePress` (deck-slots.ts): each answers whether THIS stamp
 * was marked - by a deferred load-play, or by an armed hot-cue trigger -
 * before it reached here. That import is the one exception to this module's
 * usual deck-slots avoidance (see `PerfDeck` above) - this is real
 * correlation data the ring needs, not a type this module could derive some
 * cheaper way.
 */
export function scheduleRowFacts(
	stages: Readonly<Record<string, number>>,
	contextState: string,
	pressT0Ms?: number
): { kind: ScheduleRowKind; labels: Record<string, string> } {
	return {
		kind: scheduleRowKind(
			stages.press_to_schedule_ms,
			claimLoadSpanningPress(pressT0Ms),
			claimArmedHotCuePress(pressT0Ms)
		),
		labels: latencyFloorLabels({
			unavailable: unavailableLatencyTerms({
				base: stages.base_latency_ms,
				output: stages.output_latency_ms
			}),
			contextState
		})
	};
}
