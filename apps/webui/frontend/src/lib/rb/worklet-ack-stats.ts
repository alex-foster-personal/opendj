/**
 * Q2 / PERF-R4: the distribution underneath the Signalsmith timeout cliff.
 *
 * Every AudioWorklet command in this app is a MessagePort round trip raced
 * against `withStretchCommandTimeout` (5000ms, 15000ms for creation). Until
 * now nothing recorded how long a normal ack took, so the packaged-app
 * "Signalsmith schedule timed out after 5000ms" failures arrived as a cliff
 * with nothing beneath it: no way to see 2ms drifting toward 5000ms, and no
 * way to tell a machine that is merely slow from one that is about to fail.
 *
 * WHY AGGREGATED, and why 5s. The perf ring holds 40 rows. A pitch-fader drag
 * posts one worklet schedule per pointermove, so a row per command evicts the
 * entire ring - including the deck-load rows a failure investigation actually
 * needs - within a single knob movement. Aggregation is not a nicety here, it
 * is what keeps the ring usable. 5s is chosen against the ring, not against
 * the ear: it is long enough that a sustained drag contributes one row rather
 * than dozens, and short enough that a window still sits inside one gesture,
 * so a stall shows up in the row for the gesture that caused it. The window is
 * lazy - it exists only while commands flow, and a silent app emits nothing.
 *
 * A single ack over `WORKLET_ACK_SLOW_MS` ALSO gets its own immediate row.
 * The aggregate answers "is this machine healthy"; the immediate event
 * answers "what was happening at 21:04:11 when the deck stuttered", and
 * waiting up to 5s to say so would break the correlation it exists for.
 *
 * Failed commands are recorded with their duration like any other. A 5000ms
 * timeout is the single most informative sample in the set: dropping it would
 * leave the max blind to the exact approach this instrument was built to see.
 */

import { recordPerfEvent, recordPerfTiming } from '$lib/rb/perf-event-log';

/** One aggregated row at most this often, and only while commands flow. */
export const WORKLET_ACK_FLUSH_MS = 5_000;

/** An ack above this gets its own immediate row as well as its window slot. */
export const WORKLET_ACK_SLOW_MS = 250;

const ROUND = 1000;

function _round(ms: number): number {
	return Math.round(ms * ROUND) / ROUND;
}

//-----------------------------------------------------------------------------
// pure arithmetic
//-----------------------------------------------------------------------------

/**
 * Nearest-rank percentile over an ASCENDING sample array.
 *
 * Nearest rank rather than interpolation on purpose: every reported value is a
 * duration that really happened, so a p95 of 5000 means a command really did
 * take 5000ms. An interpolated p95 of 4750 would be a number no command ever
 * spent, which is exactly the wrong shape for a cliff detector.
 */
export function percentileMs(samplesAscending: readonly number[], quantile: number): number {
	if (samplesAscending.length === 0) {
		throw new RangeError(
			'percentileMs needs at least one sample: a percentile of nothing would report 0ms ' +
				'and read as a perfectly healthy worklet'
		);
	}
	if (!Number.isFinite(quantile) || quantile <= 0 || quantile > 1) {
		throw new RangeError(`quantile must be within (0, 1], got ${quantile}`);
	}
	const rank = Math.ceil(quantile * samplesAscending.length);
	return samplesAscending[rank - 1];
}

/** `add buffers` -> `add_buffers`, so an operation name is a stable stage key. */
export function ackOpKey(operation: string): string {
	const key = operation
		.trim()
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, '_')
		.replace(/^_+|_+$/g, '');
	if (key === '') {
		throw new RangeError(`worklet operation name yields no stage key: ${JSON.stringify(operation)}`);
	}
	return key;
}

/**
 * One flush window's samples, kept per operation.
 *
 * Per operation and never pooled: `schedule` runs thousands of times per set
 * and `add buffers` a handful, so a pooled p95 is the schedule distribution
 * with the expensive operation invisible inside it.
 */
export class WorkletAckWindow {
	readonly #samples = new Map<string, number[]>();
	#count = 0;

	get sampleCount(): number {
		return this.#count;
	}

	record(operation: string, durationMs: number): void {
		if (!Number.isFinite(durationMs) || durationMs < 0) {
			throw new RangeError(
				`worklet ack duration must be finite and non-negative, got ${durationMs}`
			);
		}
		const key = ackOpKey(operation);
		const bucket = this.#samples.get(key);
		if (bucket === undefined) this.#samples.set(key, [durationMs]);
		else bucket.push(durationMs);
		this.#count += 1;
	}

	/** Stage map for one `worklet-ack` row. Empty when nothing was recorded. */
	summarize(): Record<string, number> {
		if (this.#count === 0) return {};
		const stages: Record<string, number> = { total_n: this.#count };
		for (const key of [...this.#samples.keys()].sort()) {
			const sorted = [...(this.#samples.get(key) ?? [])].sort((a, b) => a - b);
			stages[`${key}_n`] = sorted.length;
			stages[`${key}_p50_ms`] = _round(percentileMs(sorted, 0.5));
			stages[`${key}_p95_ms`] = _round(percentileMs(sorted, 0.95));
			stages[`${key}_max_ms`] = _round(sorted[sorted.length - 1]);
		}
		return stages;
	}

	reset(): void {
		this.#samples.clear();
		this.#count = 0;
	}
}

//-----------------------------------------------------------------------------
// the live window
//-----------------------------------------------------------------------------

let _window = new WorkletAckWindow();
let _flushTimer: ReturnType<typeof setTimeout> | null = null;

/** Emit the current window if it has anything in it, then start a fresh one. */
export function flushWorkletAckWindow(): void {
	_flushTimer = null;
	const stages = _window.summarize();
	_window.reset();
	if (Object.keys(stages).length === 0) return;
	recordPerfTiming('worklet-ack', stages);
}

/**
 * Time one completed worklet round trip.
 *
 * `succeeded` is false for a timeout or a processor failure; the duration is
 * recorded either way, and only the immediate event distinguishes them - a
 * stage map of numbers has nowhere honest to put an outcome.
 */
export function recordWorkletAck(operation: string, durationMs: number, succeeded: boolean): void {
	_window.record(operation, durationMs);
	if (_flushTimer === null) {
		_flushTimer = setTimeout(flushWorkletAckWindow, WORKLET_ACK_FLUSH_MS);
	}
	if (durationMs > WORKLET_ACK_SLOW_MS) {
		const outcome = succeeded ? 'acknowledged' : 'failed';
		recordPerfEvent(
			'worklet-ack-slow',
			`Signalsmith ${operation} ${outcome} after ${_round(durationMs)}ms, over the ` +
				`${WORKLET_ACK_SLOW_MS}ms slow-ack threshold; the command timeout cliff is ` +
				'5000ms (15000ms for processor creation)'
		);
	}
}

/** Drop the live window and its pending flush. Teardown and test isolation. */
export function resetWorkletAckStats(): void {
	if (_flushTimer !== null) clearTimeout(_flushTimer);
	_flushTimer = null;
	_window = new WorkletAckWindow();
}
