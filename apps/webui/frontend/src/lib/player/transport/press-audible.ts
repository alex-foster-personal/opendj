/**
 * LATENCY-03 / Q1: the device floor a press-to-audible row is allowed to claim.
 *
 * `scheduleOffsetStages` models the instant a scheduled sample reaches the
 * audio clock. The instant a DJ actually HEARS it is later than that by the
 * output device's own floor, and this module owns the one question that
 * decides whether a row may add that floor in: is each term a measurement, or
 * is it the absence of one?
 *
 * Extracted under convention D5 (docs/perf/performance-register.md): the
 * engine keeps the call sites, the arithmetic and its refusal rule live here
 * where a test can reach them. Nothing here awaits, so a Class A control never
 * gains a wait by being measured.
 *
 * MEASURED, Wed 9 Sep 2026, one `AudioContext` per engine, oscillator started,
 * 600ms of real rendering, playwright webkit 26.5 / chromium 149.0.7827.55 on
 * this Mac. Re-run it with
 * `pnpm test:live:device-latency-floor` (tests/live/device-latency-floor.mjs),
 * which is where these readings come from, so the rule below is backed by a
 * command rather than by a number somebody once pasted into a comment:
 *
 * | engine   | baseLatency | outputLatency fresh | outputLatency running |
 * |----------|-------------|---------------------|-----------------------|
 * | WebKit   | 2.902ms     | **0**               | 15.964ms              |
 * | Chromium | 5.805ms     | **0**               | 32.000ms              |
 *
 * That zero is the whole reason this module exists, and it is a NASTIER
 * failure than the missing property it was expected to be. `outputLatency` is
 * present, is a `number`, and is finite, so every guard that asks "can I read
 * this?" says yes. It simply reads 0 until the context has rendered. A row
 * taken before then reports `output_latency_ms: 0` and a reader adding the
 * device floor back gets the press latency UNDERSTATED by 16ms on the shipped
 * engine, silently, with nothing in the row to say so. The first play of a
 * session is exactly such a schedule: `_resumeContext()` runs on that press.
 *
 * And the context's own STATE cannot stand in for the check. Chromium reports
 * `running` while `outputLatency` is still 0 (WebKit reports `suspended`), so
 * a guard that trusted the state would pass exactly one of the two engines
 * shipped against.
 *
 * So the rule is an INVARIANT, not a platform table: a device floor of exactly
 * zero is not a measurement, it is the absence of one. No real output path has
 * zero latency. That stays true on engines nobody has tested here, and it
 * cannot rot the way a recorded per-engine value would.
 */

/** A device-floor stage key a row may have to go without. */
export type LatencyFloorTerm = 'base_latency_ms' | 'output_latency_ms';

/** The ring kind for a schedule NO operator press is behind. */
export const SCHEDULE_KIND = 'transport-schedule';

/**
 * The ring kind for a schedule a press IS behind.
 *
 * A SUFFIX of `SCHEDULE_KIND`, and that is load-bearing twice over. Every
 * existing consumer matches these rows by `startsWith('transport-schedule')`
 * (`_bucketOf`, the probe's python mirror, the e2e console grep), so a suffix
 * keeps all of them whole while `_bucketOf` can still give press rows a budget
 * of their own by testing this longer kind FIRST.
 *
 * The budget is the point. `transport-schedule` is the ring's noisy bucket:
 * PitchFader drives `_scheduleDeck` from an unthrottled pointermove, so one
 * fader drag is roughly 40 rows against a 16-row budget. A DJ starts a track
 * and then reaches for the pitch fader to beatmatch it - that is the standard
 * gesture, not an edge case - so the press row that just measured the thing
 * the whole program is about was reliably evicted, by the operator, within
 * about a second of making it. Same eviction this module's own file header
 * documents being fixed at the ring level, recurring one level down.
 */
export const PRESS_SCHEDULE_KIND = 'transport-schedule-press';

/**
 * The ring kind for a press-behind schedule whose wait spans a deck load.
 *
 * A SUFFIX of `PRESS_SCHEDULE_KIND` (itself a suffix of `SCHEDULE_KIND`), for
 * the same reason: `_bucketOf` matches `startsWith('transport-schedule-press')`
 * before the shorter prefix, so a load-spanning row still spends the press
 * ring's budget rather than the noisy plain one, while an S2 p95 consumer can
 * test this longest kind FIRST to exclude exactly these rows - see
 * `specs/perf-latency-program.md` S2. Deferred load-play (Space or a direct
 * Load+Play on a still-decoding track) only ever dispatches its play AFTER
 * the load it waited on resolves, so `press_to_schedule_ms` on this kind is
 * never the ordinary schedule-queue wait S2 budgets - it is honestly, and by
 * construction, a number that can run to seconds. Recorded here on the row
 * itself so a reader never has to guess load-spanning from duration.
 */
export const PRESS_SCHEDULE_LOAD_SPAN_KIND = 'transport-schedule-press-load-span';

/** Which ring kind this schedule's row belongs under. */
export function scheduleRowKind(pressToScheduleMs: number | undefined, loadSpanning: boolean): string {
	if (pressToScheduleMs === undefined) return SCHEDULE_KIND;
	if (loadSpanning) return PRESS_SCHEDULE_LOAD_SPAN_KIND;
	return PRESS_SCHEDULE_KIND;
}

/**
 * True when a device-floor reading is a real measurement.
 *
 * Non-finite covers the platform that never implemented the property.
 * Exactly-zero covers the platform that implemented it and has not filled it
 * in yet, which is the case actually observed on both engines above.
 */
export function isMeasuredLatencyFloor(seconds: number | undefined): boolean {
	if (typeof seconds !== 'number') return false;
	if (!Number.isFinite(seconds)) return false;
	if (seconds <= 0) return false;
	return true;
}

/**
 * The device-floor terms this row cannot state, in stage-key order.
 *
 * Empty means the floor is fully known and `input_to_output_ms` is earned.
 *
 * DELIBERATELY UNIT-NEUTRAL. Only presence and sign are read, never
 * magnitude, so a caller may pass the seconds it got from the AudioContext or
 * the milliseconds already rounded into the row's stages and get the same
 * answer. `press-stamp.ts` passes the stages, precisely so the absences
 * reported here are the SAME absences the row reports rather than a second
 * opinion about them.
 */
export function unavailableLatencyTerms(input: {
	base: number | undefined;
	output: number | undefined;
}): readonly LatencyFloorTerm[] {
	const missing: LatencyFloorTerm[] = [];
	if (!isMeasuredLatencyFloor(input.base)) missing.push('base_latency_ms');
	if (!isMeasuredLatencyFloor(input.output)) missing.push('output_latency_ms');
	return missing;
}

/**
 * Non-numeric facts the row carries beside its stages.
 *
 * `latency_floor` is `complete` or `partial`, never absent, because a reader
 * has to be able to tell "this row says the floor is whole" from "this row
 * predates the field". When it is `partial`, `latency_unavailable` NAMES the
 * terms, so the row says which number is missing rather than leaving a total
 * that quietly means something smaller than it looks.
 *
 * `audio_context_state` rides along as CONTEXT for a partial floor, not as
 * its explanation. On WebKit a `suspended` state does explain the zero. On
 * Chromium it does not -- the state reads `running` and the floor is still
 * unfilled -- so the label tells a reader what the context was, and nothing
 * downstream may infer the floor from it.
 */
export function latencyFloorLabels(input: {
	unavailable: readonly LatencyFloorTerm[];
	contextState: string;
}): Record<string, string> {
	if (input.unavailable.length === 0) {
		return { latency_floor: 'complete', audio_context_state: input.contextState };
	}
	return {
		latency_floor: 'partial',
		latency_unavailable: input.unavailable.join(','),
		audio_context_state: input.contextState
	};
}

/**
 * Input event to sound leaving the output, in ms, or nothing.
 *
 * The sum `input_to_audible_ms` stops one term short of: it adds the device
 * floor the sample still has to cross after the scheduled start time. That is
 * the number the operator's ears actually measure.
 *
 * Returns `undefined` rather than a partial sum whenever ANY term is missing,
 * and that refusal is the whole contract. A press-to-output figure with the
 * output stage quietly dropped is not a smaller version of this number, it is
 * a wrong one, and it is wrong in the flattering direction - which is the one
 * direction a budget instrument must never fail in. The row still carries
 * `input_to_audible_ms` and the labels above, so a reader loses the total and
 * is TOLD they lost it, instead of being handed a total that lies.
 */
export function inputToOutputMs(input: {
	pressToScheduleMs: number | undefined;
	scheduledOffsetMs: number;
	baseLatencySec: number | undefined;
	outputLatencySec: number | undefined;
}): number | undefined {
	if (input.pressToScheduleMs === undefined) return undefined;
	if (!isMeasuredLatencyFloor(input.baseLatencySec)) return undefined;
	if (!isMeasuredLatencyFloor(input.outputLatencySec)) return undefined;
	if (!Number.isFinite(input.scheduledOffsetMs)) return undefined;
	const floorMs = ((input.baseLatencySec as number) + (input.outputLatencySec as number)) * 1000;
	return input.pressToScheduleMs + input.scheduledOffsetMs + floorMs;
}
