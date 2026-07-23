/**
 * Library BPM font heat vs master - black→white, distinct from Camelot rainbow.
 *
 * Non-linear: relative error raised to 0.45 so near-matches stay bright longer,
 * then fall off toward near-black by ~10% detune. Half/double tempos fold in
 * (64 vs 128 reads as "close" for DJ mixing).
 *
 * Sketch (react / tune):
 *   0% Δ     → near white  #f0f2f5
 *   ~1%      → light gray
 *   ~3%      → mid gray
 *   ~6%      → dark gray
 *   ≥~10%    → near black  #1a1c20
 *   no master / null bpm → null (caller keeps default text color)
 */

const NEAR_WHITE = { r: 240, g: 242, b: 245 } as const;
const NEAR_BLACK = { r: 26, g: 28, b: 32 } as const;
/** Relative detune at which we hit near-black. */
const FULL_SPAN = 0.1;
/** Ease exponent (<1 = soft near zero, steeper later). */
const CURVE = 0.45;
const TEMPO_FOLDS = [1, 0.5, 2] as const;

function _relDelta(bpm: number, master: number): number {
	let best = Infinity;
	for (const fold of TEMPO_FOLDS) {
		const ref = master * fold;
		if (ref <= 0) continue;
		best = Math.min(best, Math.abs(bpm - ref) / ref);
	}
	return best;
}

/** CSS rgb() for BPM text, or null when heat does not apply. */
export function bpmHeatColor(bpm: number | null, masterBpm: number | null): string | null {
	if (bpm === null || masterBpm === null || !(masterBpm > 0) || !(bpm > 0)) return null;
	const t = Math.min(1, Math.pow(_relDelta(bpm, masterBpm) / FULL_SPAN, CURVE));
	const r = Math.round(NEAR_WHITE.r + (NEAR_BLACK.r - NEAR_WHITE.r) * t);
	const g = Math.round(NEAR_WHITE.g + (NEAR_BLACK.g - NEAR_WHITE.g) * t);
	const b = Math.round(NEAR_WHITE.b + (NEAR_BLACK.b - NEAR_WHITE.b) * t);
	return `rgb(${r} ${g} ${b})`;
}
