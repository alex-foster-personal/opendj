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

import { recordPerfEvent } from '$lib/rb/perf-event-log';

/**
 * A deck, exactly as the perf ring accepts one.
 *
 * Derived from `recordPerfEvent` rather than imported from
 * `$lib/rb/deck-slots` on purpose: an instrument whose only use for a deck is
 * to forward it to the ring has no business widening that module's importer
 * count.
 */
type PerfDeck = NonNullable<Parameters<typeof recordPerfEvent>[2]>;

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
