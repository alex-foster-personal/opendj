/**
 * Library BPM font heat vs master - bright→dim, distinct from Camelot rainbow.
 *
 * Non-linear: relative error raised to 0.45 so near-matches stay bright longer,
 * then fall off toward the readability floor by ~10% detune. Half/double
 * tempos fold in (64 vs 128 reads as "close" for DJ mixing).
 *
 * Sketch (react / tune):
 *   0% Δ     → near white  #f0f2f5
 *   ~1%      → light gray
 *   ~3%      → mid gray
 *   ≥~10%    → the dimmest gray that is still READABLE on this lane
 *   no master / null bpm → null (caller keeps default text color)
 *
 * ## The readability floor (pin 7c0c0167cb0a, the maintainer, Wed 2 Sep 2026)
 *
 * The ramp used to end at #1a1c20 on a #14171d row: a contrast ratio of
 * 1.06:1, which is not "dim", it is invisible. Worse, the lane tints are
 * painted BEHIND this text - the sweet lane washes the cell green, the half
 * lane purple - so the cells that were hardest to read were also the ones the
 * eye was drawn to.
 *
 * So the ramp is clamped, per lane, at the dimmest point that still clears
 * WCAG 2.1 AA body text (4.5:1) against the backdrop that lane actually
 * composites onto. Clamping the ramp POSITION rather than lightening the
 * final colour is what keeps "brighter = closer to master" true: the ramp is
 * monotonic in t, so clipping t preserves the ordering instead of folding the
 * far end back over the near end.
 *
 * KNOWN GAP, deliberately not covered here: a row can also be master, loaded,
 * selected or hovered, each of which paints a lighter row background under the
 * lane tint. Those backdrops are lighter still, so a few rows can sit under
 * the floor. Enumerating the full lane x row-state matrix (and the global
 * cross-project contrast requirement the maintainer asked for in the same pin) is
 * tracked separately; this covers the default row, which is where all but a
 * handful of rows live.
 */

const NEAR_WHITE = { r: 240, g: 242, b: 245 } as const;
/** Ramp target BEFORE the readability clamp. The clamp is what stops the
 * ramp ever reaching it; it stays as the direction of travel. */
const NEAR_BLACK = { r: 26, g: 28, b: 32 } as const;
/** Relative detune at which we hit the ramp's dim end. */
const FULL_SPAN = 0.1;
/** Ease exponent (<1 = soft near zero, steeper later). */
const CURVE = 0.45;
const TEMPO_FOLDS = [1, 0.5, 2] as const;

// The floor the clamps below were derived under is WCAG 2.1 AA for body text,
// 4.5:1 - the BPM cell is 11px tabular numerals, which is body text by every
// definition, so the 3:1 large-text allowance does not apply. The number itself
// lives in tests/unit/contrast.mjs (WCAG_AA_BODY_TEXT) next to the assertion
// that enforces it, rather than as a constant here that nothing would read.

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
/**
 * IOPIN-11's default master-relative compatibility tolerance.  This is an
 * absolute distance after choosing the closest of raw, half and double tempo;
 * eight exactly is intentionally not compatible (the pin says "under 8").
 */
export const BPM_COMPATIBILITY_ABS = 8.0;

export type BpmCompatibilitySeverity = 'compatible' | 'neutral' | 'warn' | 'danger' | 'critical';

export type BpmCompatibility = {
	/** 1 = raw tempo, 0.5 = half, 2 = double. */
	fold: number;
	/** Distance from the closest raw/half/double master relationship. */
	absDelta: number;
	compatible: boolean;
	/** Red-border escalation after 8, 16 and 24 BPM. */
	severity: BpmCompatibilitySeverity;
};

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

/**
 * The furthest along the ramp each lane may go and still clear
 * WCAG AA body text (4.5:1) on its own backdrop.
 *
 * LITERALS, not a runtime binary search: the ramp is a rounded 8-bit lerp of
 * the module constants above, so these are fully determined at design time,
 * and computing them in the browser would put a search loop plus the whole of
 * WCAG's arithmetic on a /performance surface that is at its gzip ratchet.
 *
 * What keeps them honest is tests/unit/bpm-contrast.test.mjs, which sweeps
 * every BPM from 40 to 300 against three masters and measures the colour this
 * module actually returns against each lane's real backdrop. A literal that
 * drifted high would put a rendered colour under the floor and fail that
 * sweep; the arithmetic and the backdrops live in tests/unit/contrast.mjs so
 * that none of it reaches the bundle.
 */
const MAX_READABLE_T: Record<BpmHeatLane, number> = {
	sweet: 0.42757004499435425,
	half: 0.4929906129837036,
	mid: 0.5350466966629028,
	far: 0.5350466966629028
};

function _colorFromRel(rel: number, lane: BpmHeatLane): string {
	const t = Math.min(Math.pow(rel / FULL_SPAN, CURVE), MAX_READABLE_T[lane]);
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
 * IOPIN-11 compatibility is deliberately distinct from the old heat lanes:
 * it evaluates the nearest musically useful relationship, then applies the
 * user-visible absolute thresholds.  This makes 64.1 vs 128 compatible while
 * keeping a raw 64 BPM mismatch red when it is not a close half-time match.
 */
export function classifyBpmCompatibility(
	bpm: number | null,
	masterBpm: number | null
): BpmCompatibility | null {
	if (bpm === null || masterBpm === null || !(bpm > 0) || !(masterBpm > 0)) return null;
	const nearest = _bestFold(bpm, masterBpm);
	const absDelta = nearest.absDelta;
	if (absDelta < BPM_COMPATIBILITY_ABS) {
		return { fold: nearest.fold, absDelta, compatible: true, severity: 'compatible' };
	}
	if (absDelta <= BPM_COMPATIBILITY_ABS) {
		return { fold: nearest.fold, absDelta, compatible: false, severity: 'neutral' };
	}
	if (absDelta <= 16) return { fold: nearest.fold, absDelta, compatible: false, severity: 'warn' };
	if (absDelta <= 24) return { fold: nearest.fold, absDelta, compatible: false, severity: 'danger' };
	return { fold: nearest.fold, absDelta, compatible: false, severity: 'critical' };
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
	const signed = bpm - masterBpm;

	// The lane is decided BEFORE the colour, because the readability floor is
	// per lane: the sweet lane paints green behind this text and the half lane
	// paints purple, so the same ramp position is a different contrast on each.
	// Floating point: 128 * 1.06 can sit a hair over 6.0%.
	let out: Omit<BpmHeat, 'color'>;
	if (rel1 * 100 <= BPM_SWEET_PCT + 1e-9) {
		out = { lane: 'sweet', fold: 1, absDelta: abs1, relDelta: rel1 };
	} else if (folded.fold !== 1 && folded.absDelta <= BPM_HALF_ABS) {
		out = { lane: 'half', fold: folded.fold, absDelta: folded.absDelta, relDelta: rel };
	} else if (signed < -BPM_FAR_BELOW || signed > BPM_FAR_ABOVE) {
		out = { lane: 'far', fold: 1, absDelta: abs1, relDelta: rel1 };
	} else {
		out = { lane: 'mid', fold: 1, absDelta: abs1, relDelta: rel1 };
	}
	return { ...out, color: _colorFromRel(rel, out.lane) };
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
		default: {
			const _exhaustive: never = heat.lane;
			throw new Error(`unhandled BPM heat lane: ${String(_exhaustive)}`);
		}
	}
}

/** CSS rgb() for BPM text, or null when heat does not apply. */
export function bpmHeatColor(bpm: number | null, masterBpm: number | null): string | null {
	const heat = classifyBpmHeat(bpm, masterBpm);
	return heat?.color ?? null;
}
