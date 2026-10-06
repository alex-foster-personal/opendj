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
 *   ✔︎ ✅ 🎯 Require an explicit sync mode; BAR prefers raw cadence and folds
 *     only when nothing else locks (pin 9bf12adccb45, AGENTS.md).
 *     [if] an exact BAR anchor exists but a fold is returned [then ⛔️]
 *     [if] a fold is the only lock and BAR refuses it [then ⛔️]
 *   ✔︎ ✅ 🎯 A re-anchor tempo ramp lands phase exactly on the plan (LATENCY-06).
 *     [if] a ramped re-anchor ends off the one-step re-anchor's phase [then ⛔️]
 *   ✔︎ ✅ 🎯 BSM ON forces BAR so downbeats stay aligned over playback (DECKUX-14).
 *     [if] BSM is on and a BEAT-mode follower nearest-beat locks [then ⛔️]
 *   ✔︎ ✅ 🎯 BAR refuses an extrapolated fallback anchor (PARITY-10, issue #1777).
 *     [if] the chosen master or follower sync anchor has extrapolated === true
 *     [then ⛔️]
 *
 * No DOM, Web Audio objects, nominal track BPM, or synthetic grid fallback.
 * Tempo ratios use local interval BPM at the play-position window (60/dt),
 * never the track-mean of all intervals or the PQTZ bpm field alone.
 */
import type { AnlzBeat, AnlzCue } from '$lib/rb/anlz-types';
import {
	classifyGrid,
	gridFlagClause,
	GRID_FLAG_CONSEQUENCE,
	GRID_QUALITY_THRESHOLDS,
	type GridClass
} from '$lib/rb/grid-quality';
import type { LoopState } from '$lib/rb/deck-state-types';

// -------------------------------------------------------------- contracts

export type BeatNumber = 1 | 2 | 3 | 4;
export type SyncMode = 'beat' | 'bar';
export type TempoNormalization = 0.5 | 1 | 2;

/**
 * Largest phase-lock trim, as a fraction of the base tempo: 0.3%, 0.38 BPM at
 * 128 BPM. Below the pitch change a DJ hears on a varispeed deck (about 5
 * cents), and twice the worst grid-rounding tempo error seen on real PQTZ
 * (~0.15%, see `_windowedIntervalBpm`), so a real drift of that size is always
 * out-run. Lives here, not in phase-lock.ts, so the tempo-lock tolerance can
 * use it without an import cycle; phase-lock.ts re-exports it.
 */
export const PHASE_LOCK_MAX_TRIM = 0.003;

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
	/**
	 * A user's seek on a synced, playing follower (waveform click, hot cue,
	 * CUE): pick the anchor whose BEAT is nearest `followerPositionSec` (the
	 * clicked or cued beat), not the anchor whose phase-shifted LANDING is.
	 * The landing then sits on that beat plus the master's phase, so the
	 * follower goes where it was sent and is in phase in one move; by landing
	 * it could fall a beat early whenever the master was past mid-beat. BAR
	 * still takes only anchors on the master's beat number, so the nearest
	 * bar-aligned beat to the click. Off (the default) for every other join.
	 */
	anchorOnBeat?: boolean | undefined;
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

/** Fractional beat index of `positionSec` on `beats`, or null off the grid
 * (before the first beat or at/after the last). Shared with phase-lock.ts. */
export function gridBeatPosition(beats: readonly AnlzBeat[], positionSec: number): number | null {
	// The comparisons also refuse NaN and +-Infinity.
	if (beats.length < 2 || !(positionSec >= beats[0].t && positionSec < beats[beats.length - 1].t)) return null;
	const lo = _enclosingBeatIndex(beats, positionSec, 'position'); // in range: never throws
	const span = beats[lo + 1].t - beats[lo].t;
	if (!(span > 0)) return null;
	return lo + (positionSec - beats[lo].t) / span;
}

/** Track time of fractional beat index `index` (0 <= index <= last), the
 * inverse of `gridBeatPosition`; a whole index is that beat's exact time. */
export function beatTimeAt(beats: readonly AnlzBeat[], index: number): number {
	const i = Math.floor(index);
	const f = index - i;
	// `f &&`: a whole index never reads past the last beat.
	return beats[i].t + (f && f * (beats[i + 1].t - beats[i].t));
}

/**
 * Phase-keeping seek: a quantized seek or a beat jump on a PLAYING deck keeps
 * the deck's own beat phase AT THE INSTANT IT LANDS (round 2 hardening, NAE-19).
 *
 * The engine decides a seek's target at one context time and the schedule
 * lands at another: `_scheduleDeckSerial` moves the requested instant later
 * whenever the processor lead or a superseding pending segment needs it. A
 * target fixed in track seconds then lands late by that gap, so the deck
 * skips part of a beat. Measured in the real engine
 * (performance-beat-sync-adversarial.spec.ts): with BeatSyncMax on, a +4
 * beat jump on a playing 128 BPM master landed 165.2 ms behind its own rhythm
 * and a quantized click 169.6 ms (8.7 and 117.5 ms with it off), and the
 * phase lock then had to drag every Beat Sync follower after it.
 *
 * The landing is therefore evaluated from the playhead projected to the
 * effective landing time:
 * - a beat jump moves exactly the beats asked, fraction included, so +4 lands
 *   four grid beats on whenever it lands (inside an engaged loop too: the
 *   loop shifts by the same beats);
 * - a quantized click / CUE / hot cue lands on the snapped target beat at
 *   the beat fraction the playhead is at when it lands (Rekordbox's quantize:
 *   the groove never skips, and a synced follower stays in phase without a
 *   re-seek).
 * Off the grid (before the first beat, at or after the last) there is no
 * phase to keep and the caller lands on its fixed target as before.
 *
 * Where a deck playing at `currentSec` lands: `jumpBeats` grid beats on
 * (a beat jump), or with no jump on the beat nearest `targetSec` at the
 * playhead's own beat fraction (a quantized seek). `targetSec` with no grid,
 * when either end is off the grid, or past `endSec` (a grid can run past the
 * decoded audio). -Claude
 */
export function phaseKeepingLandingSec(
	beats: readonly AnlzBeat[] | null,
	currentSec: number,
	jumpBeats: number | null,
	targetSec: number,
	endSec: number
): number {
	if (beats === null) return targetSec;
	const current = gridBeatPosition(beats, currentSec);
	const target = jumpBeats === null ? gridBeatPosition(beats, targetSec) : 0;
	const index =
		current === null || target === null
			? -1
			: jumpBeats === null
				? Math.round(target) + (current % 1)
				: current + jumpBeats;
	const landing = index >= 0 && index <= beats.length - 1 ? beatTimeAt(beats, index) : endSec + 1;
	return landing <= endSec ? landing : targetSec;
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

/**
 * Return a stored loop's length only when both endpoints are exact PQTZ beat
 * timestamps. Rekordbox BeatLoopSize is vendor metadata that can be packed,
 * so it is deliberately not an input to this calculation.
 */
export function pqtzLoopBeatCount(
	beats: readonly Pick<AnlzBeat, 't'>[],
	inMs: number,
	outMs: number
): number | null {
	if (!Number.isFinite(inMs) || !Number.isFinite(outMs) || outMs <= inMs) return null;
	const startSec = inMs / 1000;
	const endSec = outMs / 1000;
	const startIndex = beats.findIndex((beat) => Math.abs(beat.t - startSec) < 1e-9);
	const endIndex = beats.findIndex((beat) => Math.abs(beat.t - endSec) < 1e-9);
	const count = endIndex - startIndex;
	return startIndex >= 0 && endIndex > startIndex ? count : null;
}

/** Display-only stored loop (COMPONENT-MAP 1.3: loop chips are display at
 * v1): the rekordbox active loop when one exists, engaged: false. */
export function displayLoopFrom(cues: AnlzCue[], beats: readonly AnlzBeat[]): LoopState | null {
	const active = cues.find((c) => c.active_loop && c.out_ms !== null);
	if (active === undefined || active.out_ms === null) return null;
	return {
		in_ms: active.in_ms,
		out_ms: active.out_ms,
		engaged: false,
		beat_length: pqtzLoopBeatCount(beats, active.in_ms, active.out_ms)
	};
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
	maxRatio: number,
	anchorOnBeat?: boolean
): _FollowerAnchorPlan {
	let best: _FollowerAnchorPlan | null = null;
	let bestDistance = Number.POSITIVE_INFINITY;
	// BAR's half/double anchors are held BESIDE the exact ones, not instead of
	// them (pin 9bf12adccb45). Strict BAR used to refuse them outright, which
	// made "sync these two decks" fail on a pair a DJ would happily mix; now
	// they are the fallback, chosen only when no tempoNormalization=1 anchor
	// exists at all. Preference, not permission: a pair that could always lock
	// exactly still locks exactly, so nothing that worked before changes.
	let folded: _FollowerAnchorPlan | null = null;
	let foldedDistance = Number.POSITIVE_INFINITY;
	for (let index = 0; index < beats.length - 1; index++) {
		const beat = beats[index];
		const followerBpm = _windowedIntervalBpm(beats, index);
		const rawRatio = (masterBpm * masterTempoRatio) / followerBpm;
		const tempo = _tempoRatioWithinRangeOrNull(rawRatio, minRatio, maxRatio);
		if (tempo === null) continue;
		const foldedBar = mode === 'bar' && tempo.normalization !== 1;
		// The PQTZ bar-number constraint belongs to the EXACT lock alone. A
		// folded lock has already given up the bar count - that is precisely
		// what its orange warning says - so requiring a folded candidate to
		// ALSO sit on the master's beat number throws away three quarters of
		// the phase points for a property the fold does not preserve anyway.
		// It is not merely wasteful, it moves the deck: 200 BPM master against
		// a 100 BPM follower at 0.3 s on master beat 2 has its nearest folded
		// phase point at follower 0.3 s, and the same-number filter picks
		// 0.9 s instead - a whole 100-BPM beat away. So folded candidates
		// search every beat number and exact ones keep the constraint.
		//
		// The cost is that BAR now measures the local BPM at every beat rather
		// than at one in four, because normalization is not known until after
		// `_tempoRatioWithinRangeOrNull` has run. That is the price of asking
		// the right question in the right order.
		if (mode === 'bar' && !foldedBar && beat.n !== masterBeatNumber) continue;
		const subBeatIndex = tempo.normalization === 0.5 ? masterBeatIndex & 1 : 0;
		const phaseOffsetIntervals = (subBeatIndex + beatPhase) * tempo.normalization;
		const nextBoundaryOffsetIntervals = (subBeatIndex + 1) * tempo.normalization;
		const targetPositionSec = _positionAtIntervalOffset(beats, index, phaseOffsetIntervals);
		const nextBoundarySec = _positionAtIntervalOffset(
			beats,
			index,
			nextBoundaryOffsetIntervals
		);
		if (targetPositionSec === null || nextBoundarySec === null) continue;
		const distance = Math.abs((anchorOnBeat ? beat.t : targetPositionSec) - positionSec);
		const candidate: _FollowerAnchorPlan = {
			index,
			positionSec: targetPositionSec,
			tempoRatio: tempo.ratio,
			normalization: tempo.normalization
		};
		if (foldedBar) {
			if (distance < foldedDistance) {
				foldedDistance = distance;
				folded = candidate;
			}
		} else if (distance < bestDistance) {
			bestDistance = distance;
			best = candidate;
		}
	}
	if (best !== null) return best;
	if (folded !== null) return folded;
	// The one limit that stays hard: a ratio outside the pitch range is not a
	// policy this code may relax, it is a speed the fader cannot reach. Letting
	// it through would mean playing at a tempo that never lines up - drift, not
	// funk.
	throw new RangeError(
		`follower grid has no phase-capable ${mode} anchor with tempo ratio within ` +
			`[${minRatio}, ${maxRatio}] for beat n=${masterBeatNumber}`
	);
}

// --------------------------------------------------------------- public API

/**
 * Verdict memo for `validateBeatGrid`, keyed by grid identity (PERF-GRID-01).
 *
 * Grids are never edited in place: an analysis refresh or a beatgrid edit
 * replaces the whole array, so identity plus length is the grid's version.
 * Without this, every per-frame caller (deck snapshot BPM, BeatJump's
 * enabled state, the wave row's grid checks) re-walked ~1,000 beats, and
 * through a Svelte $state proxy each field read is a tracked signal read.
 * Measured on demon-llama, two synced decks: 56% of main-thread time.
 * Failures are memoized too, so a bad grid throws the same error every call
 * without re-walking it.
 */
interface _GridVerdict {
	length: number;
	error: Error | null;
}
let _gridVerdicts = new WeakMap<readonly AnlzBeat[], _GridVerdict>();
let _gridWalks = 0;

/** Test seam: full grid walks since the last reset (memo misses). */
export function beatGridValidationWalksForTest(): number {
	return _gridWalks;
}

/** Test seam: forget every memoized verdict and zero the walk counter. */
export function resetBeatGridValidationMemoForTest(): void {
	_gridVerdicts = new WeakMap();
	_gridWalks = 0;
}

/** Validate the complete runtime shape required by PQTZ beat math. */
export function validateBeatGrid(beats: readonly AnlzBeat[]): void {
	if (!Array.isArray(beats)) throw new TypeError('beat grid must be an array');
	const memo = _gridVerdicts.get(beats);
	if (memo !== undefined && memo.length === beats.length) {
		if (memo.error !== null) throw memo.error;
		return;
	}
	_gridWalks++;
	try {
		_walkBeatGrid(beats);
	} catch (error) {
		_gridVerdicts.set(beats, { length: beats.length, error: error as Error });
		throw error;
	}
	_gridVerdicts.set(beats, { length: beats.length, error: null });
}

function _walkBeatGrid(beats: readonly AnlzBeat[]): void {
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

/** True when `positionSec` lies within one beat interval of the grid: from a
 * beat before the first beat to a beat after the last, each edge measured by
 * its own outermost interval. A grid of fewer than two beats has no interval,
 * so every position counts as on it (the old snap). */
export function positionWithinGridSpan(beats: readonly AnlzBeat[], positionSec: number): boolean {
	if (beats.length < 2) return true;
	const first = beats[0].t;
	const last = beats[beats.length - 1].t;
	const before = first - (beats[1].t - first);
	const after = last + (last - beats[beats.length - 2].t);
	return positionSec >= before && positionSec <= after;
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

/**
 * Return the exact time of the nearest real PQTZ bar downbeat (n === 1).
 *
 * BeatSyncMax stores hot cues on bars, rather than merely on the nearest beat:
 * a hand-set cue must be phase-safe before a later synchronized launch can
 * use it. Ties are stable toward the earlier downbeat, matching ordinary
 * quantization.
 */
export function quantizeToNearestDownbeat(
	beats: readonly AnlzBeat[],
	positionSec: number
): number {
	validateBeatGrid(beats);
	_assertFiniteNonNegative('positionSec', positionSec);
	const downbeats = beats.filter((beat) => beat.n === 1);
	if (downbeats.length === 0) throw new Error('PQTZ beat grid contains no downbeat');
	return downbeats[_nearestBeatIndex(downbeats, positionSec)].t;
}

/**
 * Return the exact time of the nearest grid line for the DECK's selected
 * quantize grid (pin a67bafbfc4b0): every beat (1), every bar downbeat (4,
 * assumed 4/4 - same set `quantizeToNearestDownbeat` snaps to), or every
 * OTHER bar downbeat (8 - a 2-bar grid). 'phase' never reaches here: it is
 * rejected in performance-ipc._dispatchUnknown before it can become a
 * setting change, so this function's grid parameter excludes it entirely.
 *
 * `validateBeatGrid` enforces an unbroken n=1,2,3,4 cadence with no gaps, so
 * every downbeat is exactly 4 real beats after the last: the 2-bar grid's
 * `index % 2 === 0` IS the musical bar parity here, not merely array order,
 * because the schema forbids a bar going missing mid-grid.
 *
 * A grid with no downbeat at all (0 or 1 detected - a track with sparse or
 * failed downbeat detection) degrades to the nearest available beat, or to
 * that single downbeat, rather than throwing: an approximate grid line beats
 * refusing to seek/loop at all.
 *
 * A position more than one beat outside the grid is returned unchanged
 * (SEEK-GRID-01, #5601): there is no beat there to snap to. Clamping it put a
 * 156 s seek on the last beat of a grid that stopped at 36 s while the audio
 * ran to 180 s. Every quantized seek, CUE set and manual loop endpoint goes
 * through here, so all of them get the same rule.
 */
export function quantizeToNearestGridBeat(
	beats: readonly AnlzBeat[],
	positionSec: number,
	gridBeats: 1 | 4 | 8
): number {
	validateBeatGrid(beats);
	_assertFiniteNonNegative('positionSec', positionSec);
	if (!positionWithinGridSpan(beats, positionSec)) return positionSec;
	if (gridBeats === 1) return quantizeToNearestBeat(beats, positionSec);
	validateBeatGrid(beats);
	_assertFiniteNonNegative('positionSec', positionSec);
	const downbeats = beats.filter((beat) => beat.n === 1);
	if (downbeats.length === 0) return quantizeToNearestBeat(beats, positionSec);
	if (gridBeats === 4 || downbeats.length === 1) {
		return downbeats[_nearestBeatIndex(downbeats, positionSec)].t;
	}
	const twoBarBeats = downbeats.filter((_beat, index) => index % 2 === 0);
	return twoBarBeats[_nearestBeatIndex(twoBarBeats, positionSec)].t;
}

/**
 * Return the exact time of the next real PQTZ bar downbeat at or after
 * `positionSec` - the moment a BeatSyncMax hot-cue TRIGGER (#884) defers to,
 * rather than the nearest one SAVE (above) snaps to. Once the grid runs out
 * (`positionSec` past the last downbeat, near track end) there is no future
 * phase-locked moment left, so this returns `positionSec` itself: fire now.
 */
export function nextDownbeatAtOrAfter(beats: readonly AnlzBeat[], positionSec: number): number {
	validateBeatGrid(beats);
	_assertFiniteNonNegative('positionSec', positionSec);
	const downbeats = beats.filter((beat) => beat.n === 1);
	if (downbeats.length === 0) throw new Error('PQTZ beat grid contains no downbeat');
	const index = _firstBeatAtOrAfter(downbeats, positionSec);
	return index < downbeats.length ? downbeats[index].t : positionSec;
}

/** Where an armed jump lands on the deck's own transport: a fixed position,
 * or a resolver called with the engine's LIVE presentation position inside
 * the scheduling transaction, so a published position that went stale while
 * a command queued can never pick an arm point already behind the playhead. */
export type ArmAtPosition = number | ((nowPositionSec: number) => number);

/** Resolve `armAt` against the live `nowPositionSec` and refuse a point behind
 * it: an armed jump never schedules into the past. Shared by the Web Audio
 * engine and the Rust engine's hot-cue driver so both refuse identically. */
export function resolveArmAtPosition(armAt: ArmAtPosition, nowPositionSec: number): number {
	const armAtPositionSec = typeof armAt === 'function' ? armAt(nowPositionSec) : armAt;
	if (armAtPositionSec < nowPositionSec) {
		throw new RangeError(
			`armHotCueTrigger: armAtPositionSec ${armAtPositionSec} precedes current position ${nowPositionSec}`
		);
	}
	return armAtPositionSec;
}

export type HotCueTriggerPlan = { kind: 'immediate' } | { kind: 'armed'; armAtPositionSec: number };

/**
 * Decide whether a hot-cue TRIGGER (#884) jumps immediately or waits for the
 * deck's own next downbeat.
 *
 * BeatSyncMax only protects an audible transition already in progress, so a
 * stopped deck (nothing audible to protect) and a positionSec past an engaged
 * loop's own already-tight window both jump immediately regardless of the
 * preference. Cross-deck follower re-anchoring (the OTHER meaning of
 * BeatSyncMax, for seek) is out of scope here - this is self-referential to
 * the triggering deck's own grid only.
 */
export function planHotCueTrigger(
	beatSyncMax: boolean,
	playing: boolean,
	loopEngaged: boolean,
	positionSec: number,
	beats: readonly AnlzBeat[]
): HotCueTriggerPlan {
	if (!beatSyncMax || !playing || loopEngaged) return { kind: 'immediate' };
	return { kind: 'armed', armAtPositionSec: nextDownbeatAtOrAfter(beats, positionSec) };
}

/**
 * Target position (ms) for a beat jump measured across exact PQTZ beats.
 *
 * The anchor snaps to the nearest real grid beat, then the jump counts whole
 * grid beats - never a BPM-derived duration, so a track whose tempo drifts
 * still lands exactly on a beat. A jump that would run past either end of the
 * grid lands on the first or last real beat: a track boundary is a defined
 * edge, not a masked failure, and callers render the control inert (with a
 * reason) when no movement is left.
 *
 * `positionMs` is the caller's anchor, and the engine passes the LIVE playhead
 * while a deck is playing rather than its stored `position_ms`, which only
 * advances on state pushes. Anchoring a moving deck to a stale position would
 * jump from where it was, not from where the operator hears it. The routing
 * side of that call (loop exit, follower phase sync, presentation clock) is
 * `quantizedSeek`'s, so this module stays pure and testable.
 *
 * `keepPhase` lands exactly `deltaBeats` grid beats from the anchor instead,
 * carrying its fractional phase (p of its own beat lands at p of the target
 * beat, through each beat's own interval). A PLAYING deck must not snap: a
 * jump from phase p to a beat moves `deltaBeats - p` beats, a rhythm skip in
 * the deck's own groove that also knocks every Beat Sync follower of a
 * jumping master off phase. It snaps as above (as for a paused deck) when
 * the anchor or the target is off the grid's interior: a track edge is a
 * defined stop, not a phase to keep.
 */
export function beatJumpTargetMs(
	beats: readonly AnlzBeat[],
	positionMs: number,
	deltaBeats: number,
	keepPhase = false
): number {
	validateBeatGrid(beats);
	_assertFiniteNonNegative('positionMs', positionMs);
	if (!Number.isInteger(deltaBeats) || deltaBeats === 0) {
		throw new RangeError(`deltaBeats must be a non-zero integer, got ${deltaBeats}`);
	}
	const from = keepPhase ? gridBeatPosition(beats, positionMs / 1000) : null;
	if (from !== null && from + deltaBeats >= 0 && from + deltaBeats <= beats.length - 1) {
		return beatTimeAt(beats, from + deltaBeats) * 1000;
	}
	const anchorIndex = _nearestBeatIndex(beats, positionMs / 1000);
	const targetIndex = Math.min(Math.max(anchorIndex + deltaBeats, 0), beats.length - 1);
	return beats[targetIndex].t * 1000;
}

/**
 * The seek a beat jump performs: where it lands and whether `quantizedSeek`
 * may re-snap it. A PLAYING deck (`playing`: transport running or scheduled
 * to) keeps its phase and skips the deck's own 1/4/8-beat re-snap, which
 * would otherwise erase that phase a second time; a paused deck snaps to the
 * nearest beat and is re-quantized as before. Both are clamped to the last
 * beat inside the decoded audio (`beatJumpTargetWithinDurationMs`).
 */
export function beatJumpSeekPlan(
	beats: readonly AnlzBeat[],
	anchorMs: number,
	deltaBeats: number,
	durationMs: number,
	playing: boolean
): { targetMs: number; skipGridQuantize: boolean } {
	const raw = beatJumpTargetMs(beats, anchorMs, deltaBeats, playing);
	return { targetMs: beatJumpTargetWithinDurationMs(beats, raw, durationMs), skipGridQuantize: playing };
}

/**
 * Clamp a beat-jump target to the last real grid beat at or before the
 * decoded audio duration.
 *
 * `beatJumpTargetMs` always lands on beat data from the analyzed grid, but
 * an ANLZ beatgrid can be extrapolated slightly past the decoded buffer
 * boundary for the final interval - the same overshoot
 * `loopEndpointsWithinDurationMs` documents for loops. Clamping to
 * `durationMs` itself would park the transport at an arbitrary off-grid
 * instant; clamping to the exact time of the last in-range beat keeps the
 * target ON the grid, so a caller's own re-quantization (e.g.
 * `quantizedSeek`) snaps it back to itself rather than re-selecting a beat
 * past the boundary.
 */
export function beatJumpTargetWithinDurationMs(
	beats: readonly AnlzBeat[],
	targetMs: number,
	durationMs: number
): number {
	validateBeatGrid(beats);
	_assertFiniteNonNegative('targetMs', targetMs);
	_assertFiniteNonNegative('durationMs', durationMs);
	if (targetMs <= durationMs) return targetMs;
	for (let index = beats.length - 1; index >= 0; index--) {
		if (beats[index].t * 1000 <= durationMs) return beats[index].t * 1000;
	}
	throw new RangeError(
		`no PQTZ beat at or before decoded duration ${durationMs}ms among ${beats.length} beats`
	);
}

/**
 * Whether a beat jump would actually move the transport once its target is
 * clamped to the decoded audio duration, the same clamp the engine applies
 * (`beatJumpTargetWithinDurationMs`).
 *
 * The unclamped target alone (`beatJumpTargetMs`) can differ from the anchor
 * when the only grid movement available is to a final beat that sits past
 * the decoded buffer boundary: the engine then clamps that jump back to the
 * same already-current beat, so a render-path gate using the unclamped
 * comparison would enable a button that silently no-ops. Both the anchor and
 * the target are clamped before comparing, so an anchor that itself sits
 * past duration (an edge case, but not one to assume away) is judged on the
 * same terms as its target.
 */
export function beatJumpMovesTransportWithinDuration(
	beats: readonly AnlzBeat[],
	positionMs: number,
	deltaBeats: number,
	durationMs: number
): boolean {
	try {
		const anchorIndex = _nearestBeatIndex(beats, positionMs / 1000);
		const anchorMs = beatJumpTargetWithinDurationMs(beats, beats[anchorIndex].t * 1000, durationMs);
		const targetMs = beatJumpTargetWithinDurationMs(
			beats,
			beatJumpTargetMs(beats, positionMs, deltaBeats),
			durationMs
		);
		return targetMs !== anchorMs;
	} catch {
		return false;
	}
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
export const BAR_SYNC_EXTRAPOLATED_ANCHOR =
	'bar sync refuses extrapolated downbeat at the sync anchor';

export function beatIsExtrapolated(beat: Pick<AnlzBeat, 'extrapolated'>): boolean {
	return beat.extrapolated === true;
}

export const DEFAULT_TEMPO_LOCK_TOLERANCE_BPM = 0.1;

/**
 * The default tolerance at one fold of the master tempo: the 0.1 BPM display
 * slack PLUS the largest trim the phase lock itself applies
 * (PHASE_LOCK_MAX_TRIM of the folded master BPM, 0.38 BPM at 128). A locked,
 * in-phase follower carrying an ordinary trim must not read "Off tempo"; a
 * real 1 BPM mismatch at 128 (tolerance 0.48) still does.
 */
export function tempoLockToleranceBpm(foldedMasterBpm: number): number {
	return DEFAULT_TEMPO_LOCK_TOLERANCE_BPM + PHASE_LOCK_MAX_TRIM * foldedMasterBpm;
}

/**
 * True when candidateBpm is tempo-locked to masterBpm at 1x, 0.5x, or 2x
 * within toleranceBpm - the three ratios Beat Sync itself accepts (see
 * `TempoNormalization`). Without an explicit toleranceBpm, each fold uses
 * `tempoLockToleranceBpm` so a phase-lock trim is not read as off tempo.
 * Null, non-finite, or non-positive inputs mean there is no valid reference
 * to compare against (no elected master, no live BPM yet, or the master deck
 * against itself); those cases return true so the UI never shows a mismatch
 * without a real error to report.
 */
export function isTempoLockedToMaster(
	candidateBpm: number | null,
	masterBpm: number | null,
	toleranceBpm?: number
): boolean {
	if (candidateBpm === null || masterBpm === null) return true;
	if (!Number.isFinite(candidateBpm) || candidateBpm <= 0) return true;
	if (!Number.isFinite(masterBpm) || masterBpm <= 0) return true;
	const normalizations: readonly TempoNormalization[] = [1, 0.5, 2];
	return normalizations.some((normalization) => {
		const folded = masterBpm * normalization;
		return Math.abs(candidateBpm - folded) <= (toleranceBpm ?? tempoLockToleranceBpm(folded));
	});
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
export {
	computeQuantizedLaunchArm,
	QUANTIZED_LAUNCH
} from '$lib/player/transport/quantized-launch';

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

export interface PhaseCompensatedReanchor {
	/** Follower position to schedule at the sync instant, with the first step's rate. */
	startPositionSec: number;
	steps: TempoRampStep[];
}

/**
 * The re-anchor ramp PLUS the start position that makes it phase-exact.
 *
 * `planTempoRatioRamp` eases only the RATE. A follower placed on its planned
 * anchor at the sync instant and then run short of (or past) its target rate
 * through the ramp ends it behind (or ahead of) that plan by the ramp's rate
 * shortfall - 0.1125 s of track time per unit of ratio change for the default
 * ramp - and nothing afterwards corrects it: every re-anchor since 786cc91a1
 * (Sun 16 Aug 2026) left the follower off the beat until the next one, ~10 ms
 * after a 1.00 -> 1.10 master tempo move. Starting ahead by exactly that
 * shortfall makes phase converge onto the plan at the last step and stay.
 *
 * [if] fromTempoRatio equals toTempoRatio [then] startPositionSec is the
 * anchor itself - an unchanged tempo has nothing to compensate.
 * [if] the compensated start falls before the track start [then ⛔️] - a
 * clamp would silently re-introduce the offset this exists to remove.
 */
export function planPhaseCompensatedReanchor(
	anchorPositionSec: number,
	fromTempoRatio: number,
	toTempoRatio: number
): PhaseCompensatedReanchor {
	_assertFiniteNonNegative('anchorPositionSec', anchorPositionSec);
	const steps = planTempoRatioRamp(fromTempoRatio, toTempoRatio);
	let shortfallSec = 0;
	for (let index = 0; index < steps.length - 1; index++) {
		const heldSec = steps[index + 1].offsetSec - steps[index].offsetSec;
		shortfallSec += (toTempoRatio - steps[index].tempoRatio) * heldSec;
	}
	const startPositionSec = anchorPositionSec + shortfallSec;
	_assertFiniteNonNegative('phase-compensated start position', startPositionSec);
	return { startPositionSec, steps };
}

/**
 * Plan one scheduled follower seek and playback-rate change.
 *
 * The caller projects the master through its transport and loop map to the
 * requested future context time. Both decks then use local interval BPM at
 * the play-position window, never track-mean BPM, and the follower receives
 * the master's fractional beat phase. Bar mode additionally requires equal
 * PQTZ beat numbers and raw cadence.
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
	// A master still in its intro (before its first grid beat: a pickup, a
	// count-in, silence) is read on its grid extrapolated BACKWARDS at the
	// first interval, so the follower lands where the master's first beat
	// will find it in phase. Its beat index is then negative, and the beat is
	// treated as extrapolated, so BAR (which needs a real downbeat number)
	// refuses it below as it refuses any extrapolated anchor.
	// `back`: how many first intervals the master is before its first beat (0
	// on the grid, negative in the intro); `gridIndex`: the grid beat it is
	// on (the first one in the intro). The grid is validated, so `n` is 1..4
	// and `back * interval` is exactly 0 on it.
	const grid = request.masterGrid;
	const back = Math.min(0, Math.floor((projectedMasterPositionSec - grid[0].t) / (grid[1].t - grid[0].t)));
	const gridIndex = back ? 0 : _enclosingBeatIndex(grid, projectedMasterPositionSec, 'master position');
	const masterBeatIndex = gridIndex + back;
	_enclosingBeatIndex(request.followerGrid, request.followerPositionSec, 'follower position');

	const masterBeatNumber = (((grid[gridIndex].n - 1 + back) & 3) + 1) as BeatNumber;
	const masterBeatIntervalSec = grid[gridIndex + 1].t - grid[gridIndex].t;
	const masterBeatSec = grid[gridIndex].t + back * masterBeatIntervalSec;
	const beatPhase = (projectedMasterPositionSec - masterBeatSec) / masterBeatIntervalSec;
	const masterIntervalBpm = _windowedIntervalBpm(grid, gridIndex);
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
		request.maxFollowerTempoRatio,
		request.anchorOnBeat
	);

	if (mode === 'bar') {
		if (back || beatIsExtrapolated(grid[gridIndex])) {
			throw new RangeError(BAR_SYNC_EXTRAPOLATED_ANCHOR);
		}
		if (beatIsExtrapolated(request.followerGrid[followerAnchor.index])) {
			throw new RangeError(BAR_SYNC_EXTRAPOLATED_ANCHOR);
		}
	}

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


// --------------------------------------------- grids Beat Sync may wander on

/** A beat interval further than this from the grid's median interval is
 * uneven. Above what millisecond storage alone produces (two rounded beat
 * times put an interval up to 1 ms off, and the median up to 1 ms more).
 * The number itself lives in the shared rule (`$lib/rb/grid-quality`). */
export const UNEVEN_GRID_INTERVAL_TOLERANCE_SEC = GRID_QUALITY_THRESHOLDS.unevenIntervalToleranceSec;

export interface BeatSyncGridWarning {
	/** Intervals more than UNEVEN_GRID_INTERVAL_TOLERANCE_SEC off the median. */
	unevenIntervalCount: number;
	intervalCount: number;
	/** Largest interval deviation from the median, in ms (0 when none). */
	worstDeviationMs: number;
	/** Track time of the worst interval's first beat. */
	worstAtSec: number;
	/** Beats the analyzer extrapolated instead of detecting. */
	extrapolatedBeatCount: number;
	/** What the shared rule calls this grid: `suspect` (steady music under an
	 * uneven grid), `variable_tempo`, or `ok` when only extrapolation warns. */
	gridClass: GridClass;
	/** One line for the deck: what is wrong and what it means for sync. */
	message: string;
}

/**
 * Why Beat Sync may wander on this grid, or null when it has no such reason.
 *
 * The continuous phase lock (NAE-19) holds a follower to its GRID. Where the grid's beats are
 * unevenly spaced, or were extrapolated rather than detected, the grid and
 * the audio can disagree, and holding to one lets the other be heard off the
 * beat. That is not a failure to hide: the DJ is told before they lean on it.
 *
 * The uneven half is the ONE rule the library's Err-column flag also reads
 * (`classifyGrid`, GRIDFLAG-01), so a track flagged in the library says the
 * same sentence here. Extrapolated beats are a deck-only addition: they are a
 * property of the loaded grid's beats, which the stored library verdict does
 * not carry.
 *
 * Deliberately not `detectBeatgridIssue`: that compares the PQTZ `bpm` FIELD
 * to the intervals (a data-quality column), while sync never reads the field.
 *
 * [if] the grid is missing or too short to have an interval [then] null -
 * that state already has its own gridless tip.
 */
export function beatSyncGridWarning(beats: readonly AnlzBeat[]): BeatSyncGridWarning | null {
	if (!Array.isArray(beats) || beats.length < 2) return null;
	const quality = classifyGrid(
		beats.map((beat) => beat.t),
		beats.map((beat) => beat.bpm)
	);
	let extrapolatedBeatCount = 0;
	for (const beat of beats) if (beatIsExtrapolated(beat)) extrapolatedBeatCount += 1;
	const flagged = quality.gridClass === 'suspect' || quality.gridClass === 'variable_tempo';
	if (!flagged && extrapolatedBeatCount === 0) return null;
	const reasons: string[] = [];
	if (flagged) reasons.push(gridFlagClause(quality));
	if (extrapolatedBeatCount > 0) {
		reasons.push(`${extrapolatedBeatCount} of ${beats.length} beats are extrapolated, not detected`);
	}
	return {
		unevenIntervalCount: quality.unevenIntervalCount,
		intervalCount: quality.intervalCount,
		worstDeviationMs: quality.worstDeviationMs,
		worstAtSec: quality.worstAtSec,
		extrapolatedBeatCount,
		gridClass: quality.gridClass,
		message: `Beatgrid: ${reasons.join('; ')} - ${GRID_FLAG_CONSEQUENCE}`
	};
}

// ----------------------------------------------- what the sync tells the DJ

/**
 * A completed Beat Sync has two things it may need to say, and both are pure
 * functions of the plan it produced, so they are decided here rather than in
 * the engine: the engine keeps the effects (`pushToast`, `recordPerfEvent`)
 * and this returns the DATA describing them. That split is what lets the
 * wording, the severity and the grouping key be asserted with no audio graph,
 * no browser and no fixture library - see beat-sync-notices.test.mjs.
 *
 * Deck ids are a type parameter rather than an import: nothing here inspects
 * a deck id beyond printing it, so `DeckId` flows through from the caller and
 * this module keeps its existing dependency set.
 */

/** One perf-event row a notice wants written. */
export interface BeatSyncNoticeEvent<D extends number> {
	kind: 'beat-sync-fold' | 'beat-sync-skip';
	detail: string;
	deck: D;
}

/** One toast a notice wants raised, plus the rows that accompany it. */
export interface BeatSyncNotice<D extends number> {
	message: string;
	/**
	 * One severity for BOTH the toast and its perf rows, deliberately.
	 * `recordPerfEvent`'s severity argument DEFAULTS to 'warn', so the
	 * engine's old three-argument call filed a beat-sync SKIP - a red toast,
	 * a follower that never locked - into the ring as a warning that could
	 * never reach `_escalate()`. Carrying it here is what stops the colour
	 * the DJ sees and the severity the ring records from drifting apart.
	 */
	kind: 'warn' | 'error';
	/** Repeat presses against the same master coalesce onto one toast. */
	groupKey: string;
	events: BeatSyncNoticeEvent<D>[];
}

/** The slice of a planned follower these notices read. */
export interface PlannedFollower<D extends number> {
	deck: D;
	plan: Pick<FollowerSyncPlan, 'mode' | 'tempoNormalization'>;
}

/** A follower that could not be planned at all, with the refusal's reason. */
export interface FailedFollower<D extends number> {
	deck: D;
	message: string;
}

/**
 * Pin 9bf12adccb45: BAR folding to half/double is now allowed, so the DJ is
 * TOLD rather than blocked. Orange, not red, and not silent: the decks really
 * are phase-locked, but one is counting bars at twice or half the other's
 * rate, which is audible and deliberate.
 *
 * A follower planned in BEAT mode is never a fold notice however its tempo
 * was normalized - BEAT never promised a bar count in the first place, so
 * there is nothing there for the DJ to be surprised by.
 */
export function beatSyncOutcomeNotices<D extends number>(
	planned: readonly PlannedFollower<D>[],
	planFailed: readonly FailedFollower<D>[],
	master: D
): BeatSyncNotice<D>[] {
	const notices: BeatSyncNotice<D>[] = [];
	const folded = planned.filter(
		(item) => item.plan.mode === 'bar' && item.plan.tempoNormalization !== 1
	);
	if (folded.length > 0) {
		const detail = folded
			.map(
				(item) =>
					`deck ${item.deck} at ${item.plan.tempoNormalization === 0.5 ? 'half' : 'double'} tempo`
			)
			.join(', ');
		notices.push({
			message: `BAR sync locked with a tempo fold (${detail}) - phase holds, the bar count does not`,
			kind: 'warn',
			groupKey: `beat-sync-fold:${master}`,
			events: folded.map((item) => ({
				kind: 'beat-sync-fold' as const,
				detail: `tempoNormalization=${item.plan.tempoNormalization}`,
				deck: item.deck
			}))
		});
	}
	if (planFailed.length > 0) {
		const skipped = planFailed.map((f) => f.deck).join(',');
		notices.push({
			message: `Beat Sync skipped deck(s) [${skipped}] (tempo/phase cannot lock) - others stayed locked`,
			kind: 'error',
			groupKey: `beat-sync-followers:${master}`,
			events: planFailed.map((f) => ({
				kind: 'beat-sync-skip' as const,
				detail: f.message,
				deck: f.deck
			}))
		});
	}
	return notices;
}
