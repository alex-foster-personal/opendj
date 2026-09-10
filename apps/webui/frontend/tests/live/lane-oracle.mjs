/**
 * The live run's independent verdict on which lane SHOULD have been chosen.
 *
 * The live check exists to catch a calibration that picked the slower lane, so
 * its oracle has to apply the same rule the implementation does. It did not:
 * it called any worker win above 1.0x a win, while production declines a win
 * inside `LANE_MARGIN` on purpose, so a correct margin-protected decision was
 * reported as DISAGREE and failed the run.
 *
 * The margin is READ out of the implementation rather than copied here. A
 * copied constant rots the first time the real one moves, and an oracle that
 * silently disagrees with the code it audits is worse than no oracle.
 *
 * Its own module so both halves are reachable from the unit suite: the runner
 * needs two browsers and gigabytes of audio, and a regex that stops matching
 * is exactly the kind of defect that would otherwise surface as a confident
 * wrong verdict rather than as an error.
 */

/**
 * The margin the implementation uses, or null when the source does not say.
 *
 * Null is never defaulted to a plausible 1.25 by a caller: a margin that could
 * not be read is a comparison that cannot be made, and guessing would let a
 * renamed or moved constant pass silently against a stale number.
 *
 * @param {string} source
 * @returns {number | null}
 */
export function laneMarginFrom(source) {
	const match = source.match(/LANE_MARGIN\s*=\s*([\d.]+)/);
	if (match === null) return null;
	const margin = Number(match[1]);
	// A margin below 1 would mean the workers win by losing, which is not a
	// margin this oracle can honor and is more likely a bad match than a
	// deliberate value.
	return Number.isFinite(margin) && margin >= 1 ? margin : null;
}

/**
 * Which lane the stopwatch says, under the implementation's own margin.
 *
 * @param {number} faster main-thread ms divided by worker ms
 * @param {number} margin
 * @returns {'workers' | 'main-thread'}
 */
export function stopwatchLane(faster, margin) {
	return faster > margin ? 'workers' : 'main-thread';
}
