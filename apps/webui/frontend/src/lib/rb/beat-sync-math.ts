/**
 * Pure beat-grid quantize and deck-sync math.
 *
 * Requirements:
 *   ✔︎ ✅ 🎯 Validate real PQTZ beats before using them.
 *     [if] a grid is missing, short, unordered, or malformed [then ⛔️]
 *   ✔︎ ✅ 🎯 Quantize to the exact nearest beat with earlier-tie stability.
 *     [if] a position is equidistant between beats [then] return the earlier t
 *   ✔︎ ✅ 🎯 Plan follower phase and local tempo at one future context time.
 *     [if] no raw, half-time, or double-time ratio fits [then ⛔️]
 *   ✔︎ ✅ 🎯 Require an explicit sync mode and preserve raw cadence in BAR mode.
 *     [if] BAR needs half/double normalization [then ⛔️] select BEAT instead
 *
 * No DOM, Web Audio objects, nominal track BPM, or synthetic grid fallback.
 * Tempo ratios use beat INTERVALS (60/dt), never the PQTZ bpm field alone -
 * that field can disagree with .t (Proper Education: field 124.72 vs dt→125).
 */
import type { AnlzBeat } from '$lib/rb/types';

// -------------------------------------------------------------- contracts

export type BeatNumber = 1 | 2 | 3 | 4;
export type SyncMode = 'beat' | 'bar';
export type TempoNormalization = 0.5 | 1 | 2;

export interface FollowerSyncRequest {
	masterGrid: readonly AnlzBeat[];
	followerGrid: readonly AnlzBeat[];
	/** Loop-aware master track position already projected to syncAtContextTimeSec. */
	masterPositionAtSyncSec: number;
	/** Current master AudioBufferSourceNode playbackRate. */
	masterTempoRatio: number;
	/** Follower track position used to select the least-distant anchor. */
	followerPositionSec: number;
	currentContextTimeSec: number;
	syncAtContextTimeSec: number;
	minFollowerTempoRatio: number;
	maxFollowerTempoRatio: number;
	/** Beat matches the nearest beat; bar also requires the same PQTZ n. */
	mode: SyncMode;
}

export interface FollowerSyncPlan {
	mode: SyncMode;
	syncAtContextTimeSec: number;
	/** Loop-aware master position supplied at syncAtContextTimeSec. */
	masterPositionSec: number;
	masterBeatIndex: number;
	followerBeatIndex: number;
	masterBeatNumber: BeatNumber;
	/** Fraction through the enclosing master beat interval, within [0, 1). */
	beatPhase: number;
	/** Follower seek position to apply at syncAtContextTimeSec. */
	followerPositionSec: number;
	/** Follower playbackRate to apply at syncAtContextTimeSec. */
	followerTempoRatio: number;
	/** Multiplier applied to the raw local PQTZ BPM ratio. */
	tempoNormalization: TempoNormalization;
}

// --------------------------------------------------------------- helpers

function _assertFiniteNonNegative(name: string, value: number): void {
	if (!Number.isFinite(value) || value < 0) {
		throw new RangeError(`${name} must be finite and >= 0, got ${value}`);
	}
}

function _assertFinitePositive(name: string, value: number): void {
	if (!Number.isFinite(value) || value <= 0) {
		throw new RangeError(`${name} must be finite and > 0, got ${value}`);
	}
}

function _firstBeatAtOrAfter(beats: readonly AnlzBeat[], positionSec: number): number {
	let lo = 0;
	let hi = beats.length;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (beats[mid].t < positionSec) {
			lo = mid + 1;
		} else {
			hi = mid;
		}
	}
	return lo;
}

function _nearestBeatIndex(beats: readonly AnlzBeat[], positionSec: number): number {
	const laterIndex = _firstBeatAtOrAfter(beats, positionSec);
	if (laterIndex === 0) return 0;
	if (laterIndex === beats.length) return beats.length - 1;
	const earlierIndex = laterIndex - 1;
	const earlierDistance = positionSec - beats[earlierIndex].t;
	const laterDistance = beats[laterIndex].t - positionSec;
	return earlierDistance <= laterDistance ? earlierIndex : laterIndex;
}

function _enclosingBeatIndex(
	beats: readonly AnlzBeat[],
	positionSec: number,
	positionName: string
): number {
	const firstTimeSec = beats[0].t;
	const lastTimeSec = beats[beats.length - 1].t;
	if (positionSec < firstTimeSec || positionSec >= lastTimeSec) {
		throw new RangeError(
			`${positionName} ${positionSec} is outside the beat grid interval ` +
				`[${firstTimeSec}, ${lastTimeSec})`
		);
	}
	const atOrAfterIndex = _firstBeatAtOrAfter(beats, positionSec);
	if (beats[atOrAfterIndex].t === positionSec) return atOrAfterIndex;
	return atOrAfterIndex - 1;
}

function _tempoRatioWithinRangeOrNull(
	rawRatio: number,
	minRatio: number,
	maxRatio: number
): { ratio: number; normalization: TempoNormalization } | null {
	const candidates: readonly TempoNormalization[] = [1, 0.5, 2];
	for (const normalization of candidates) {
		const ratio = rawRatio * normalization;
		if (ratio >= minRatio && ratio <= maxRatio) return { ratio, normalization };
	}
	return null;
}

function _positionAtIntervalOffset(
	beats: readonly AnlzBeat[],
	anchorIndex: number,
	intervalOffset: number
): number | null {
	const wholeIntervals = Math.floor(intervalOffset);
	const fraction = intervalOffset - wholeIntervals;
	const intervalIndex = anchorIndex + wholeIntervals;
	if (intervalIndex >= beats.length) return null;
	if (fraction === 0) return beats[intervalIndex].t;
	if (intervalIndex + 1 >= beats.length) return null;
	return (
		beats[intervalIndex].t +
		fraction * (beats[intervalIndex + 1].t - beats[intervalIndex].t)
	);
}

/** Half-window (beats) for tempo from intervals; absorbs dirty PQTZ gaps. */
const _INTERVAL_BPM_RADIUS = 32;
/** Drop intervals farther than this fraction from the window median. */
const _INTERVAL_BPM_OUTLIER = 0.06;

function _medianSorted(sorted: readonly number[]): number {
	const mid = Math.floor(sorted.length / 2);
	return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function _mean(values: readonly number[]): number {
	let sum = 0;
	for (const value of values) sum += value;
	return sum / values.length;
}

/**
 * Tempo from a robust average of nearby beat intervals. PQTZ `bpm` is advisory
 * only; stretch locks must match wall-clock spacing from `.t`. Proper
 * Education has field 124.72 vs dt→125, plus multi-beat 114-138 BPM gaps -
 * those must not yank follower rate on waveform seek.
 *
 * Two aggregation steps, because two DIFFERENT grid defects fight each other:
 *   1. A preliminary MEDIAN centres a +-6% band that rejects the sparse gross
 *      outliers of a dirty grid (Proper Education's 114-138 BPM gaps).
 *   2. The MEAN of the survivors then recovers the true tempo. rekordbox
 *      stores beat `.t` to the millisecond, so a genuinely constant track's
 *      per-beat interval DITHERS between the two ms values straddling the
 *      true 60/bpm (e.g. 130 BPM -> 0.461s/0.462s -> 130.15/129.87 BPM). Their
 *      MEAN is the true 130.00; a MEDIAN would snap to whichever rounded value
 *      is more frequent, biasing tempo by up to ~0.15 BPM. That bias is the
 *      deck's DISPLAYED and follower-LOCK BPM, while the master's real audio
 *      still plays its true tempo - so two decks with identical displayed BPM
 *      drift apart (PURA VIDA 130 master vs Yotto 126 follower: median lock
 *      129.87 vs real 130.00 = ~0.18 BPM, one beat every ~5.5 min).
 */
function _windowedIntervalBpm(beats: readonly AnlzBeat[], index: number): number {
	if (beats.length < 2) {
		throw new RangeError(`interval BPM requires at least 2 beats, got ${beats.length}`);
	}
	const center = index >= beats.length - 1 ? beats.length - 2 : Math.max(0, index);
	const lo = Math.max(0, center - _INTERVAL_BPM_RADIUS);
	const hi = Math.min(beats.length - 1, center + _INTERVAL_BPM_RADIUS + 1);
	const bpms: number[] = [];
	for (let i = lo; i < hi; i++) {
		const dt = beats[i + 1].t - beats[i].t;
		if (dt > 0 && Number.isFinite(dt)) bpms.push(60 / dt);
	}
	if (bpms.length === 0) {
		throw new RangeError(`no positive beat intervals near index ${center}`);
	}
	bpms.sort((left, right) => left - right);
	const preliminary = _medianSorted(bpms);
	const loBpm = preliminary * (1 - _INTERVAL_BPM_OUTLIER);
	const hiBpm = preliminary * (1 + _INTERVAL_BPM_OUTLIER);
	const filtered = bpms.filter((bpm) => bpm >= loBpm && bpm <= hiBpm);
	if (filtered.length === 0) return preliminary;
	return _mean(filtered);
}

interface _FollowerAnchorPlan {
	index: number;
	positionSec: number;
	tempoRatio: number;
	normalization: TempoNormalization;
}

function _bestFollowerAnchor(
	beats: readonly AnlzBeat[],
	positionSec: number,
	mode: SyncMode,
	masterBeatIndex: number,
	masterBeatNumber: BeatNumber,
	beatPhase: number,
	masterBpm: number,
	masterTempoRatio: number,
	minRatio: number,
	maxRatio: number
): _FollowerAnchorPlan {
	let best: _FollowerAnchorPlan | null = null;
	let bestDistance = Number.POSITIVE_INFINITY;
	const rejectedBarNormalizations = new Set<TempoNormalization>();
	for (let index = 0; index < beats.length - 1; index++) {
		const beat = beats[index];
		if (mode === 'bar' && beat.n !== masterBeatNumber) continue;
		const followerBpm = _windowedIntervalBpm(beats, index);
		const rawRatio = (masterBpm * masterTempoRatio) / followerBpm;
		const tempo = _tempoRatioWithinRangeOrNull(rawRatio, minRatio, maxRatio);
		if (tempo === null) continue;
		if (mode === 'bar' && tempo.normalization !== 1) {
			rejectedBarNormalizations.add(tempo.normalization);
			continue;
		}
		const subBeatIndex = tempo.normalization === 0.5 ? masterBeatIndex % 2 : 0;
		const phaseOffsetIntervals = (subBeatIndex + beatPhase) * tempo.normalization;
		const nextBoundaryOffsetIntervals = (subBeatIndex + 1) * tempo.normalization;
		const targetPositionSec = _positionAtIntervalOffset(beats, index, phaseOffsetIntervals);
		const nextBoundarySec = _positionAtIntervalOffset(
			beats,
			index,
			nextBoundaryOffsetIntervals
		);
		if (targetPositionSec === null || nextBoundarySec === null) continue;
		const distance = Math.abs(targetPositionSec - positionSec);
		if (distance < bestDistance) {
			bestDistance = distance;
			best = {
				index,
				positionSec: targetPositionSec,
				tempoRatio: tempo.ratio,
				normalization: tempo.normalization
			};
		}
	}
	if (best === null) {
		if (mode === 'bar' && rejectedBarNormalizations.size > 0) {
			const requiredNormalizations = [...rejectedBarNormalizations]
				.sort((left, right) => left - right)
				.map((normalization) => `tempoNormalization=${normalization}`)
				.join(' or ');
			throw new RangeError(
				`strict BAR sync requires tempoNormalization=1 to preserve raw PQTZ cadence; ` +
				`the available anchor requires ${requiredNormalizations}. ` +
				`Select BEAT mode for half/double tempo matching or widen the follower tempo range.`
			);
		}
		throw new RangeError(
			`follower grid has no phase-capable ${mode} anchor with tempo ratio within ` +
				`[${minRatio}, ${maxRatio}] for beat n=${masterBeatNumber}`
		);
	}
	return best;
}

// --------------------------------------------------------------- public API

/** Validate the complete runtime shape required by PQTZ beat math. */
export function validateBeatGrid(beats: readonly AnlzBeat[]): void {
	if (!Array.isArray(beats)) throw new TypeError('beat grid must be an array');
	if (beats.length < 2) {
		throw new RangeError(`beat grid must contain at least 2 beats, got ${beats.length}`);
	}
	let previousTimeSec = -1;
	for (let index = 0; index < beats.length; index++) {
		const beat = beats[index];
		if (typeof beat !== 'object' || beat === null) {
			throw new TypeError(`beat[${index}] must be an object`);
		}
		if (!Number.isInteger(beat.n) || beat.n < 1 || beat.n > 4) {
			throw new RangeError(`beat[${index}].n must be an integer within 1..4, got ${beat.n}`);
		}
		if (!Number.isFinite(beat.bpm) || beat.bpm <= 0) {
			throw new RangeError(`beat[${index}].bpm must be finite and > 0, got ${beat.bpm}`);
		}
		if (!Number.isFinite(beat.t) || beat.t < 0) {
			throw new RangeError(`beat[${index}].t must be finite and >= 0, got ${beat.t}`);
		}
		if (index > 0 && beat.t <= previousTimeSec) {
			throw new RangeError(
				`beat grid times must be strictly increasing: beat[${index - 1}].t=` +
					`${previousTimeSec}, beat[${index}].t=${beat.t}`
			);
		}
		if (index > 0) {
			const previousBeatNumber = beats[index - 1].n;
			const expectedBeatNumber = previousBeatNumber === 4 ? 1 : previousBeatNumber + 1;
			if (beat.n !== expectedBeatNumber) {
				throw new RangeError(
					`beat grid n cadence must cycle 1,2,3,4: beat[${index}].n=${beat.n}, ` +
						`expected ${expectedBeatNumber}`
				);
			}
		}
		previousTimeSec = beat.t;
	}
}

/** Return the exact t of the nearest beat. Equidistant ties choose earlier. */
export function quantizeToNearestBeat(
	beats: readonly AnlzBeat[],
	positionSec: number
): number {
	validateBeatGrid(beats);
	_assertFiniteNonNegative('positionSec', positionSec);
	return beats[_nearestBeatIndex(beats, positionSec)].t;
}

/** Local tempo (BPM) from the nearest beat's interval; null when unusable. */
export function gridBpmAt(
	beats: readonly AnlzBeat[],
	positionSec: number
): number | null {
	try {
		validateBeatGrid(beats);
		_assertFiniteNonNegative('positionSec', positionSec);
		return _windowedIntervalBpm(beats, _nearestBeatIndex(beats, positionSec));
	} catch {
		return null;
	}
}

/**
 * Playback tempo in BPM for UI / IPC readouts.
 *
 * Beat Sync plans tempo from beat intervals (60/dt), not tag BPM or the
 * PQTZ bpm field (field can lie; Proper Education field 124.72 vs dt→125).
 * Prefer interval BPM × tempo ratio; fall back to tag only with no grid.
 */
export function playbackBpm(input: {
	beats: readonly AnlzBeat[] | null | undefined;
	positionSec: number;
	tempoRatio: number;
	tagBpm: number | null;
}): number | null {
	if (!Number.isFinite(input.tempoRatio) || input.tempoRatio <= 0) {
		return null;
	}
	const gridBpm =
		input.beats === null || input.beats === undefined
			? null
			: gridBpmAt(input.beats, input.positionSec);
	const baseBpm = gridBpm ?? input.tagBpm;
	if (baseBpm === null || !Number.isFinite(baseBpm) || baseBpm <= 0) {
		return null;
	}
	return baseBpm * input.tempoRatio;
}

/** Default absolute BPM tolerance for {@link isTempoLockedToMaster}.
 * Wider than pure float noise (~1e-9) because two decks compute their
 * windowed interval BPM independently at their own evolving position, and
 * each track's local grid jitter (dirty PQTZ, filtered by
 * `_INTERVAL_BPM_OUTLIER`) can still differ by a few hundredths of a BPM
 * between decks even while genuinely tempo-locked. A real mismatch (wrong
 * track, unsynced tempo) differs by whole BPM digits, so 0.1 stays far
 * below that while absorbing normal windowed-interval jitter without
 * flickering the UI. */
export const DEFAULT_TEMPO_LOCK_TOLERANCE_BPM = 0.1;

/**
 * True when candidateBpm is tempo-locked to masterBpm at 1x, 0.5x, or 2x
 * within toleranceBpm - the three ratios Beat Sync itself accepts (see
 * `TempoNormalization`). Null, non-finite, or non-positive inputs mean
 * there is no valid reference to compare against (no elected master, no
 * live BPM yet, or the master deck against itself); those cases return
 * true so the UI never shows a mismatch without a real error to report.
 */
export function isTempoLockedToMaster(
	candidateBpm: number | null,
	masterBpm: number | null,
	toleranceBpm: number = DEFAULT_TEMPO_LOCK_TOLERANCE_BPM
): boolean {
	if (candidateBpm === null || masterBpm === null) return true;
	if (!Number.isFinite(candidateBpm) || candidateBpm <= 0) return true;
	if (!Number.isFinite(masterBpm) || masterBpm <= 0) return true;
	const normalizations: readonly TempoNormalization[] = [1, 0.5, 2];
	return normalizations.some(
		(normalization) => Math.abs(candidateBpm - masterBpm * normalization) <= toleranceBpm
	);
}

// ------------------------------------------ beatgrid data-quality (Err col)

export type BeatgridIssueSeverity = 'warning' | 'error';

export interface BeatgridIssue {
	severity: BeatgridIssueSeverity;
	/** Beat time (seconds) of the worst disagreement found. */
	atSec: number;
	/** PQTZ `bpm` field at that beat. */
	fieldBpm: number;
	/** True tempo implied by the adjacent beat interval (60 / dt). */
	intervalBpm: number;
	/** abs(fieldBpm - intervalBpm). */
	disagreementBpm: number;
}

/** Field vs. interval BPM disagreement above this is worth a look (orange). */
export const BEATGRID_ISSUE_WARN_BPM = 2;
/** Above this, the disagreement is large enough to audibly jump tempo (red). */
export const BEATGRID_ISSUE_ERROR_BPM = 5;
/**
 * A run of this many or fewer consecutive same-direction outlier beats is
 * isolated noise (e.g. a transient-detection artifact at a filter-sweep or
 * drop - real captured data confirms Proper Education's dirty ~120-130s
 * region runs up to 5 beats deep). A longer run means the interval genuinely
 * kept drifting one way for many beats in a row - a real sustained tempo
 * change in the music, not a data-quality problem - so it is not flagged.
 */
export const BEATGRID_ISSUE_MAX_RUN = 6;

/**
 * Worst ISOLATED PQTZ field-vs-interval BPM disagreement in the grid.
 *
 * A bare per-beat threshold cannot tell a noisy artifact from a real tempo
 * change, since both produce a beat where field and interval disagree. The
 * distinguishing signal is run length: noise is 1-few beats surrounded by
 * otherwise-agreeing neighbors; a real tempo change drifts the same
 * direction for many consecutive beats. So a candidate only counts when its
 * run of consecutive same-direction disagreements is at most
 * BEATGRID_ISSUE_MAX_RUN beats long.
 *
 * Mirrors apps/webui/server/beatgrid_diagnostics.py's `detect_beatgrid_issue`
 * (backend computes it once per real ANLZ parse and caches the verdict;
 * this copy exists so the pure math is unit-tested here too and reusable
 * without a network round-trip). Returns null when every beat's field bpm
 * is within tolerance of its own interval, every disagreement is part of a
 * longer sustained drift, or the grid is too short to have an interval at
 * all - all real "nothing to flag" states, never a guessed issue.
 */
export function detectBeatgridIssue(beats: readonly AnlzBeat[]): BeatgridIssue | null {
	if (!Array.isArray(beats) || beats.length < 2) return null;
	const intervalCount = beats.length - 1;
	const intervalBpms: number[] = new Array(intervalCount);
	const disagreements: number[] = new Array(intervalCount);
	for (let i = 0; i < intervalCount; i++) {
		const dt = beats[i + 1].t - beats[i].t;
		const intervalBpm = dt > 0 && Number.isFinite(dt) ? 60 / dt : Number.NaN;
		intervalBpms[i] = intervalBpm;
		disagreements[i] = Number.isFinite(intervalBpm) ? beats[i].bpm - intervalBpm : 0;
	}

	// -1/0/1: which side of the reference this beat's field lies on, or 0
	// when it agrees closely enough to not count as an outlier at all.
	function direction(i: number): -1 | 0 | 1 {
		const d = disagreements[i];
		if (Math.abs(d) <= BEATGRID_ISSUE_WARN_BPM) return 0;
		return d > 0 ? 1 : -1;
	}

	// Length of the maximal run of consecutive same-direction outliers
	// containing beat i (a lone outlier has run length 1).
	function runLength(i: number): number {
		const dir = direction(i);
		let start = i;
		while (start > 0 && direction(start - 1) === dir) start--;
		let end = i;
		while (end < intervalCount - 1 && direction(end + 1) === dir) end++;
		return end - start + 1;
	}

	let worst: BeatgridIssue | null = null;
	for (let i = 0; i < intervalCount; i++) {
		if (direction(i) === 0) continue;
		if (runLength(i) > BEATGRID_ISSUE_MAX_RUN) continue;
		const disagreementBpm = Math.abs(disagreements[i]);
		if (worst !== null && disagreementBpm <= worst.disagreementBpm) continue;
		worst = {
			severity: disagreementBpm > BEATGRID_ISSUE_ERROR_BPM ? 'error' : 'warning',
			atSec: beats[i].t,
			fieldBpm: beats[i].bpm,
			intervalBpm: intervalBpms[i],
			disagreementBpm
		};
	}
	return worst;
}

// --------------------------------------------------- re-anchor rate ramp

/**
 * Total time to approach a re-anchored followerTempoRatio. Grid noise
 * (the residual the windowed median above cannot fully absorb) is at most
 * a few hundredths of a BPM at typical 120-140 BPM tempos - under 0.1%
 * relative rate. Spread across this window, each step's rate change stays
 * far below the ~0.1-0.3% pitch-change JND, so isolated noise is inaudible
 * while still settling well within a DJ's sense of an "instant" control
 * (the ~150-400ms range industry mixers use for tempo-fader smoothing).
 */
export const REANCHOR_RAMP_DURATION_SEC = 0.25;
/** Step cadence: fast enough to read as continuous, matching this engine's
 * other control-rate constants (e.g. its 25ms context-wait poll). */
export const REANCHOR_RAMP_STEP_SEC = 0.025;

export interface TempoRampStep {
	/** Seconds after the ramp's own sync instant; the first step is 0. */
	offsetSec: number;
	tempoRatio: number;
}

/**
 * Rate-limited correction, the DSP-side counterpart to Mixxx's
 * `calcSyncAdjustment`: never jump straight to a recomputed target ratio,
 * always approach it through small steps. Unlike Mixxx's open-ended
 * per-tick phase-error nudge, the target here is already known (one
 * recomputed `followerTempoRatio`, not a live error signal), so this lands
 * on it exactly at a fixed, bounded duration instead of an unbounded
 * correction loop.
 *
 * [if] fromTempoRatio equals toTempoRatio [then] a single no-op step -
 * nothing to ramp, and the caller still gets a step to schedule.
 */
export function planTempoRatioRamp(
	fromTempoRatio: number,
	toTempoRatio: number,
	durationSec: number = REANCHOR_RAMP_DURATION_SEC,
	stepSec: number = REANCHOR_RAMP_STEP_SEC
): TempoRampStep[] {
	_assertFinitePositive('fromTempoRatio', fromTempoRatio);
	_assertFinitePositive('toTempoRatio', toTempoRatio);
	_assertFinitePositive('durationSec', durationSec);
	_assertFinitePositive('stepSec', stepSec);
	if (stepSec > durationSec) {
		throw new RangeError(`stepSec ${stepSec} must not exceed durationSec ${durationSec}`);
	}
	if (fromTempoRatio === toTempoRatio) return [{ offsetSec: 0, tempoRatio: toTempoRatio }];
	const stepCount = Math.max(1, Math.round(durationSec / stepSec));
	return Array.from({ length: stepCount }, (_unused, index) => ({
		offsetSec: index * stepSec,
		tempoRatio:
			index === stepCount - 1
				? toTempoRatio
				: fromTempoRatio + (toTempoRatio - fromTempoRatio) * ((index + 1) / stepCount)
	}));
}

/**
 * Plan one scheduled follower seek and playback-rate change.
 *
 * The caller projects the master through its transport and loop map to the
 * requested future context time. Both decks then use local PQTZ BPM at their
 * anchor, and the follower receives the master's fractional beat phase. Bar
 * mode additionally requires equal PQTZ beat numbers and raw cadence.
 */
export function computeFollowerSyncPlan(request: FollowerSyncRequest): FollowerSyncPlan {
	validateBeatGrid(request.masterGrid);
	validateBeatGrid(request.followerGrid);
	_assertFiniteNonNegative('masterPositionAtSyncSec', request.masterPositionAtSyncSec);
	_assertFinitePositive('masterTempoRatio', request.masterTempoRatio);
	_assertFiniteNonNegative('followerPositionSec', request.followerPositionSec);
	_assertFiniteNonNegative('currentContextTimeSec', request.currentContextTimeSec);
	_assertFiniteNonNegative('syncAtContextTimeSec', request.syncAtContextTimeSec);
	if (request.syncAtContextTimeSec <= request.currentContextTimeSec) {
		throw new RangeError(
			`syncAtContextTimeSec must be after currentContextTimeSec, got ` +
				`${request.syncAtContextTimeSec} <= ${request.currentContextTimeSec}`
		);
	}
	_assertFinitePositive('minFollowerTempoRatio', request.minFollowerTempoRatio);
	_assertFinitePositive('maxFollowerTempoRatio', request.maxFollowerTempoRatio);
	if (request.minFollowerTempoRatio > request.maxFollowerTempoRatio) {
		throw new RangeError(
			`follower tempo ratio range is inverted: ` +
				`${request.minFollowerTempoRatio} > ${request.maxFollowerTempoRatio}`
		);
	}
	const mode = request.mode;
	if (mode !== 'beat' && mode !== 'bar') {
		throw new TypeError(`mode must be "beat" or "bar", got ${String(mode)}`);
	}

	const projectedMasterPositionSec = request.masterPositionAtSyncSec;
	if (!Number.isFinite(projectedMasterPositionSec)) {
		throw new RangeError(`projected master position is not finite: ${projectedMasterPositionSec}`);
	}
	const masterBeatIndex = _enclosingBeatIndex(
		request.masterGrid,
		projectedMasterPositionSec,
		'master position'
	);
	_enclosingBeatIndex(request.followerGrid, request.followerPositionSec, 'follower position');

	const masterBeat = request.masterGrid[masterBeatIndex];
	const masterBeatNumber = masterBeat.n as BeatNumber;
	const masterBeatIntervalSec = request.masterGrid[masterBeatIndex + 1].t - masterBeat.t;
	const beatPhase = (projectedMasterPositionSec - masterBeat.t) / masterBeatIntervalSec;
	const masterIntervalBpm = _windowedIntervalBpm(request.masterGrid, masterBeatIndex);
	const followerAnchor = _bestFollowerAnchor(
		request.followerGrid,
		request.followerPositionSec,
		mode,
		masterBeatIndex,
		masterBeatNumber,
		beatPhase,
		masterIntervalBpm,
		request.masterTempoRatio,
		request.minFollowerTempoRatio,
		request.maxFollowerTempoRatio
	);

	return {
		mode,
		syncAtContextTimeSec: request.syncAtContextTimeSec,
		masterPositionSec: projectedMasterPositionSec,
		masterBeatIndex,
		followerBeatIndex: followerAnchor.index,
		masterBeatNumber,
		beatPhase,
		followerPositionSec: followerAnchor.positionSec,
		followerTempoRatio: followerAnchor.tempoRatio,
		tempoNormalization: followerAnchor.normalization
	};
}
