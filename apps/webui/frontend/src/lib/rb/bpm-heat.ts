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

/**
 * BPM matching tolerances, shared with next-only-filter.ts.
 *
 * SWEET_PCT mirrors ``apps.shared.harmonic.MAX_BPM_DIFF_PCT`` (6.0). It is
 * DUPLICATED here rather than imported because the browser cannot import
 * Python; tests/test_bpm_tolerances_match_python.py fails if the two drift.
 *
 * HALF_ABS is the absolute window for half/double folds (TrackTable purple
 * lane). Absolute rather than relative on purpose: at a fold the relative
 * error is 100% by construction, so a percentage window is meaningless there.
 */
export const BPM_SWEET_PCT = 6.0;
/** Half/double lane window (absolute BPM). Unit tests: 180↔90 / ±15. */
export const BPM_HALF_ABS = 15.0;
/** Below-master absolute BPM past which the cell goes far/red. */
export const BPM_FAR_BELOW = 25.0;
/** Above-master absolute BPM past which the cell goes far/red. */
export const BPM_FAR_ABOVE = 30.0;

export type BpmHeatLane = 'sweet' | 'half' | 'mid' | 'far';

export type BpmHeat = {
	lane: BpmHeatLane;
	fold: number;
	absDelta: number;
	relDelta: number;
	color: string;
};

function _relDelta(bpm: number, master: number): number {
	let best = Infinity;
	for (const fold of TEMPO_FOLDS) {
		const ref = master * fold;
		if (ref <= 0) continue;
		best = Math.min(best, Math.abs(bpm - ref) / ref);
	}
	return best;
}

function _colorFromRel(rel: number): string {
	const t = Math.min(1, Math.pow(rel / FULL_SPAN, CURVE));
	const r = Math.round(NEAR_WHITE.r + (NEAR_BLACK.r - NEAR_WHITE.r) * t);
	const g = Math.round(NEAR_WHITE.g + (NEAR_BLACK.g - NEAR_WHITE.g) * t);
	const b = Math.round(NEAR_WHITE.b + (NEAR_BLACK.b - NEAR_WHITE.b) * t);
	return `rgb(${r} ${g} ${b})`;
}

function _bestFold(bpm: number, master: number): { fold: number; absDelta: number } {
	let best = { fold: 1, absDelta: Math.abs(bpm - master) };
	for (const fold of TEMPO_FOLDS) {
		const ref = master * fold;
		const abs = Math.abs(bpm - ref);
		if (abs < best.absDelta) best = { fold, absDelta: abs };
	}
	return best;
}

/**
 * Classify BPM vs master for TrackTable cell classes + tooltip.
 * Priority: sweet (1x ±SWEET_PCT) → half/double (±HALF_ABS) → far → mid.
 */
export function classifyBpmHeat(
	bpm: number | null,
	masterBpm: number | null
): BpmHeat | null {
	if (bpm === null || masterBpm === null || !(masterBpm > 0) || !(bpm > 0)) return null;
	const rel1 = Math.abs(bpm - masterBpm) / masterBpm;
	const abs1 = Math.abs(bpm - masterBpm);
	const folded = _bestFold(bpm, masterBpm);
	const rel = _relDelta(bpm, masterBpm);
	const color = _colorFromRel(rel);

	// Floating point: 128 * 1.06 can sit a hair over 6.0%.
	if (rel1 * 100 <= BPM_SWEET_PCT + 1e-9) {
		return { lane: 'sweet', fold: 1, absDelta: abs1, relDelta: rel1, color };
	}
	if (folded.fold !== 1 && folded.absDelta <= BPM_HALF_ABS) {
		return {
			lane: 'half',
			fold: folded.fold,
			absDelta: folded.absDelta,
			relDelta: rel,
			color
		};
	}
	const signed = bpm - masterBpm;
	if (signed < -BPM_FAR_BELOW || signed > BPM_FAR_ABOVE) {
		return { lane: 'far', fold: 1, absDelta: abs1, relDelta: rel1, color };
	}
	return { lane: 'mid', fold: 1, absDelta: abs1, relDelta: rel1, color };
}

/** Tooltip for a classified heat cell. */
export function bpmHeatLabel(heat: BpmHeat | null, masterBpm: number | null): string | null {
	if (heat === null || masterBpm === null) return null;
	const master = masterBpm.toFixed(1);
	switch (heat.lane) {
		case 'sweet':
			return `BPM within ${BPM_SWEET_PCT}% of master ${master}`;
		case 'half':
			return heat.fold === 0.5
				? `half-tempo of master ${master} (±${BPM_HALF_ABS} BPM)`
				: `double-tempo of master ${master} (±${BPM_HALF_ABS} BPM)`;
		case 'mid':
			return `BPM mid-range vs master ${master}`;
		case 'far':
			return `BPM far from master ${master}`;
		default:
			return null;
	}
}

/** CSS rgb() for BPM text, or null when heat does not apply. */
export function bpmHeatColor(bpm: number | null, masterBpm: number | null): string | null {
	const heat = classifyBpmHeat(bpm, masterBpm);
	return heat?.color ?? null;
}
