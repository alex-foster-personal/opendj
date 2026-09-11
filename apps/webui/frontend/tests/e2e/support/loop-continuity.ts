/**
 * Loop-continuity invariant for an engaged loop's presented playhead.
 *
 * The question this answers is "did the DJ-visible cursor keep moving for the
 * WHOLE observation", not "how many wraps did the sampler happen to catch".
 * Two earlier shapes of that assertion were wrong in opposite directions:
 *
 * - Counting backward adjacent samples counts WRAPS, and `position_ms` is
 *   published on requestAnimationFrame rather than at each audio loop
 *   boundary, so a sparse pair can span several real wraps while appearing to
 *   move forward. The count aliases with the loop period.
 * - Max-minus-min over the whole observation measures the SPREAD OF PHASES the
 *   sampler happened to see. It aliases with the loop period too (a healthy
 *   loop can present 700ms then 900ms), and it is satisfied by movement early
 *   in the window even if the deck stalls or clamps at loop-out for the rest
 *   of it.
 *
 * So the invariant here is neither. The observation is cut into consecutive
 * wall-clock slices, ANCHORED AT ITS END so a tail stall can never fall into a
 * discarded remainder, and every slice must show wrap-aware forward advance.
 * Nothing counts wraps and nothing counts distinct phases: the only inputs are
 * elapsed wall-clock time and the presented position, so the assertion is
 * independent of the presentation cadence beyond one requirement it states
 * openly - the cursor must move at least once per slice. A cursor frozen for a
 * whole second is the frozen-playhead defect itself, which is why the engine
 * substitutes the sample clock when the output timestamp stalls
 * (.planning/hardening-ledger/decisions/presentation-clock-fallback.md).
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 A stall or a clamp ANYWHERE in the observation fails, including
 *     the last slice.
 *     [if] a deck advances for 2s then freezes for 3s [then ⛔️] no slice is
 *       reported stalled
 *   ✔︎ ✅ 🎯 The verdict never depends on how many phases or wraps were
 *     sampled.
 *     [if] the rule reads a wrap count or a max-minus-min spread [then ⛔️] it
 *       aliases with the loop period again
 *   ✔︎ ✅ 🎯 A slice too sparsely sampled to judge is an ERROR, never a pass.
 *     [if] a one-sample slice scores zero advance and reads as a stall
 *       [then ⛔️] a starved observer is reported as a broken deck
 */

export interface PresentedLoopSample {
	readonly observedAtMs: number;
	readonly positionMs: number;
}

export interface LoopContinuitySlice {
	readonly startedAtMs: number;
	readonly endedAtMs: number;
	readonly sampleCount: number;
	/** Adjacent pairs whose presented position moved at all. Diagnostic only. */
	readonly changeCount: number;
	readonly advanceMs: number;
}

export interface LoopContinuityOptions {
	/** Wall-clock length of one slice. Must be shorter than the loop. */
	readonly sliceMs: number;
	readonly loopLengthMs: number;
	/** Floor on a slice's advance. A stall or a clamp scores exactly zero, so
	 * this only has to clear noise, not approach the slice length. */
	readonly minimumAdvanceMs: number;
}

function _finite(label: string, value: number): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new TypeError(`${label} must be a finite number, got ${String(value)}`);
	}
	return value;
}

/**
 * Forward advance between two presented positions of a bounded loop.
 *
 * A loop cursor can only decrease by wrapping, so a decrease is credited with
 * the wrap. A SMALL decrease is clock noise rather than a wrap: crediting it
 * would turn a frozen cursor jittering by 2ms into a nearly full loop of fake
 * progress, which is exactly the stall this file exists to catch.
 */
export function wrapAwareAdvanceMs(fromMs: number, toMs: number, loopLengthMs: number): number {
	_finite('fromMs', fromMs);
	_finite('toMs', toMs);
	if (_finite('loopLengthMs', loopLengthMs) <= 0) {
		throw new RangeError(`loopLengthMs must be > 0, got ${loopLengthMs}`);
	}
	const rawMs = toMs - fromMs;
	if (rawMs >= 0) return rawMs;
	if (-rawMs * 2 > loopLengthMs) return rawMs + loopLengthMs;
	return 0;
}

function _validate(
	samples: readonly PresentedLoopSample[],
	options: LoopContinuityOptions
): void {
	if (samples.length < 2) {
		throw new RangeError(`loop continuity needs at least 2 samples, got ${samples.length}`);
	}
	if (_finite('sliceMs', options.sliceMs) <= 0) {
		throw new RangeError(`sliceMs must be > 0, got ${options.sliceMs}`);
	}
	if (_finite('loopLengthMs', options.loopLengthMs) <= 0) {
		throw new RangeError(`loopLengthMs must be > 0, got ${options.loopLengthMs}`);
	}
	if (options.sliceMs >= options.loopLengthMs) {
		// A slice longer than the loop can hide a whole loop of advance inside
		// one modular step, which would make the advance ambiguous.
		throw new RangeError(
			`sliceMs (${options.sliceMs}) must be shorter than loopLengthMs (${options.loopLengthMs})`
		);
	}
	if (_finite('minimumAdvanceMs', options.minimumAdvanceMs) < 0) {
		throw new RangeError(`minimumAdvanceMs must be >= 0, got ${options.minimumAdvanceMs}`);
	}
	if (options.minimumAdvanceMs > options.sliceMs) {
		throw new RangeError(
			`minimumAdvanceMs (${options.minimumAdvanceMs}) cannot exceed sliceMs (${options.sliceMs})`
		);
	}
	let previousAtMs = Number.NEGATIVE_INFINITY;
	for (const sample of samples) {
		_finite('observedAtMs', sample.observedAtMs);
		_finite('positionMs', sample.positionMs);
		if (sample.observedAtMs < previousAtMs) {
			throw new RangeError(
				`samples must be ordered by observedAtMs, got ${sample.observedAtMs} after ${previousAtMs}`
			);
		}
		previousAtMs = sample.observedAtMs;
	}
}

function _sliceOf(
	samples: readonly PresentedLoopSample[],
	startedAtMs: number,
	endedAtMs: number,
	loopLengthMs: number
): LoopContinuitySlice {
	const inSlice = samples.filter(
		(sample) => sample.observedAtMs >= startedAtMs && sample.observedAtMs <= endedAtMs
	);
	if (inSlice.length < 2) {
		// Not a stall: the OBSERVER went quiet. Reporting it as a stalled deck
		// would blame the app for the sampler's gap, so it fails loudly instead.
		throw new RangeError(
			`slice ${startedAtMs.toFixed(0)}..${endedAtMs.toFixed(0)}ms holds ` +
				`${inSlice.length} sample(s); the observer must sample faster than the slice`
		);
	}
	let advanceMs = 0;
	let changeCount = 0;
	for (let index = 1; index < inSlice.length; index += 1) {
		const previousMs = inSlice[index - 1].positionMs;
		const currentMs = inSlice[index].positionMs;
		if (currentMs !== previousMs) changeCount += 1;
		advanceMs += wrapAwareAdvanceMs(previousMs, currentMs, loopLengthMs);
	}
	return {
		startedAtMs,
		endedAtMs,
		sampleCount: inSlice.length,
		changeCount,
		advanceMs
	};
}

/**
 * Consecutive slices of the observation, oldest first, anchored at its END.
 *
 * Anchoring at the end is the point: whatever remainder does not fill a whole
 * slice is dropped from the START, so a deck that stops advancing in the final
 * second is always inside a judged slice.
 */
export function sliceLoopContinuity(
	samples: readonly PresentedLoopSample[],
	options: LoopContinuityOptions
): LoopContinuitySlice[] {
	_validate(samples, options);
	const firstAtMs = samples[0].observedAtMs;
	const lastAtMs = samples[samples.length - 1].observedAtMs;
	const slices: LoopContinuitySlice[] = [];
	for (
		let endedAtMs = lastAtMs;
		endedAtMs - options.sliceMs >= firstAtMs;
		endedAtMs -= options.sliceMs
	) {
		slices.push(
			_sliceOf(samples, endedAtMs - options.sliceMs, endedAtMs, options.loopLengthMs)
		);
	}
	if (slices.length === 0) {
		throw new RangeError(
			`observation spans ${(lastAtMs - firstAtMs).toFixed(0)}ms, ` +
				`too short for a ${options.sliceMs}ms continuity slice`
		);
	}
	return slices.reverse();
}

/** The slices that failed the advance floor. Empty means the loop kept moving. */
export function findStalledLoopSlices(
	samples: readonly PresentedLoopSample[],
	options: LoopContinuityOptions
): LoopContinuitySlice[] {
	return sliceLoopContinuity(samples, options).filter(
		(slice) => slice.advanceMs < options.minimumAdvanceMs
	);
}

/** One human-readable line per stalled slice, for the assertion message. */
export function describeStalledLoopSlices(slices: readonly LoopContinuitySlice[]): string {
	return slices
		.map(
			(slice) =>
				`[${slice.startedAtMs.toFixed(0)}..${slice.endedAtMs.toFixed(0)}ms] ` +
				`advanced ${slice.advanceMs.toFixed(0)}ms over ${slice.sampleCount} samples ` +
				`(${slice.changeCount} moved)`
		)
		.join('; ');
}
