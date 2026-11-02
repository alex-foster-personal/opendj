/**
 * Phase-error measurement for a preview snap plan (PVSNAP spike).
 *
 * Shared by the unit test and the live Chromium harness so both report the
 * same number the same way: for an instant after the preview starts, how far
 * the preview's beat phase sits from the master's, in milliseconds of the
 * master's beat, and whether the two are on the same beat number.
 *
 * Both positions are linear from the start instant (constant rates), which is
 * what a preview source and an un-nudged master actually do. Positive error
 * means the preview is AHEAD of the master.
 */

/** Wrap a beat fraction difference into [-0.5, 0.5). */
export function wrapBeats(d) {
	return d - Math.floor(d + 0.5);
}

/**
 * @param {object} p
 * @param {(beats: object[], sec: number) => number | null} p.gridBeatPosition
 * @param {object[]} p.masterGrid
 * @param {object[]} p.previewGrid
 * @param {number} p.masterPositionAtStartSec
 * @param {number} p.masterTempoRatio
 * @param {{ startAtContextSec: number, offsetSec: number, rate: number }} p.plan
 * @param {number} p.atContextSec
 */
export function phaseErrorAt(p) {
	const dt = p.atContextSec - p.plan.startAtContextSec;
	const masterSec = p.masterPositionAtStartSec + dt * p.masterTempoRatio;
	const previewSec = p.plan.offsetSec + dt * p.plan.rate;
	const mb = p.gridBeatPosition(p.masterGrid, masterSec);
	const pb = p.gridBeatPosition(p.previewGrid, previewSec);
	if (mb === null || pb === null) return null;
	const mi = Math.floor(mb);
	const pi = Math.floor(pb);
	const masterBeatWallSec = (p.masterGrid[mi + 1].t - p.masterGrid[mi].t) / p.masterTempoRatio;
	const errBeats = wrapBeats((pb - pi) - (mb - mi));
	// The beat each side is NEAREST to after the wrap, so a preview a hair
	// behind a downbeat still counts as on it.
	const masterN = p.masterGrid[(mi + (mb - mi >= 0.5 ? 1 : 0))]?.n;
	const previewN = p.previewGrid[(pi + (pb - pi >= 0.5 ? 1 : 0))]?.n;
	return { errorMs: errBeats * masterBeatWallSec * 1000, sameBeatNumber: masterN === previewN };
}

/** A constant-tempo grid: `count` beats at `bpm`, first beat at `offset`,
 * first beat number `firstN`. Invented data, never a library track. */
export function constantGrid(count, bpm, offset = 0, firstN = 1) {
	const beat = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({
		n: ((firstN - 1 + i) % 4) + 1,
		bpm,
		t: offset + i * beat
	}));
}
