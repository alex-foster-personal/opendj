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
 *
 * No DOM, Web Audio objects, nominal track BPM, or synthetic grid fallback.
 */
import type { AnlzBeat } from '$lib/rb/types';

// -------------------------------------------------------------- contracts

export type BeatNumber = 1 | 2 | 3 | 4;
export type SyncMode = 'beat' | 'bar';
export type TempoNormalization = 0.5 | 1 | 2;

export interface FollowerSyncRequest {
	masterGrid: readonly AnlzBeat[];
	followerGrid: readonly AnlzBeat[];
	/** Master track position measured at currentContextTimeSec. */
	masterPositionSec: number;
	/** Current master AudioBufferSourceNode playbackRate. */
	masterTempoRatio: number;
	/** Follower track position used to select the least-distant anchor. */
	followerPositionSec: number;
	currentContextTimeSec: number;
	syncAtContextTimeSec: number;
	minFollowerTempoRatio: number;
	maxFollowerTempoRatio: number;
	/** Beat matches the nearest beat; bar also requires the same PQTZ n. */
	mode?: SyncMode;
}

export interface FollowerSyncPlan {
	mode: SyncMode;
	syncAtContextTimeSec: number;
	/** Master position after projection to syncAtContextTimeSec. */
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

function _nearestFollowerAnchorIndex(
	beats: readonly AnlzBeat[],
	positionSec: number,
	mode: SyncMode,
	masterBeatNumber: BeatNumber
): number {
	let nearestIndex = -1;
	let nearestDistance = Number.POSITIVE_INFINITY;
	// The final beat cannot anchor a fractional phase because it has no
	// following interval. Earlier-index ties remain stable by using <.
	for (let index = 0; index < beats.length - 1; index++) {
		if (mode === 'bar' && beats[index].n !== masterBeatNumber) continue;
		const distance = Math.abs(beats[index].t - positionSec);
		if (distance < nearestDistance) {
			nearestDistance = distance;
			nearestIndex = index;
		}
	}
	if (nearestIndex < 0) {
		throw new RangeError(
			`follower grid has no phase-capable ${mode} anchor for beat n=${masterBeatNumber}`
		);
	}
	return nearestIndex;
}

function _tempoRatioWithinRange(
	rawRatio: number,
	minRatio: number,
	maxRatio: number
): { ratio: number; normalization: TempoNormalization } {
	const candidates: readonly TempoNormalization[] = [1, 0.5, 2];
	for (const normalization of candidates) {
		const ratio = rawRatio * normalization;
		if (ratio >= minRatio && ratio <= maxRatio) return { ratio, normalization };
	}
	throw new RangeError(
		`no follower tempo ratio fits [${minRatio}, ${maxRatio}]: ` +
			`raw=${rawRatio}, half=${rawRatio * 0.5}, double=${rawRatio * 2}`
	);
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

/**
 * Plan one scheduled follower seek and playback-rate change.
 *
 * The master is projected from the shared input clock to the requested
 * future context time. Both decks then use local PQTZ BPM at their anchor,
 * and the follower receives the master's fractional beat phase. Bar mode
 * additionally requires equal PQTZ beat numbers.
 */
export function computeFollowerSyncPlan(request: FollowerSyncRequest): FollowerSyncPlan {
	validateBeatGrid(request.masterGrid);
	validateBeatGrid(request.followerGrid);
	_assertFiniteNonNegative('masterPositionSec', request.masterPositionSec);
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
	const mode = request.mode === undefined ? 'beat' : request.mode;
	if (mode !== 'beat' && mode !== 'bar') {
		throw new TypeError(`mode must be "beat" or "bar", got ${String(mode)}`);
	}

	const contextLeadSec = request.syncAtContextTimeSec - request.currentContextTimeSec;
	const projectedMasterPositionSec =
		request.masterPositionSec + contextLeadSec * request.masterTempoRatio;
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
	const followerBeatIndex = _nearestFollowerAnchorIndex(
		request.followerGrid,
		request.followerPositionSec,
		mode,
		masterBeatNumber
	);
	const followerBeat = request.followerGrid[followerBeatIndex];
	const followerBeatIntervalSec =
		request.followerGrid[followerBeatIndex + 1].t - followerBeat.t;
	const followerPositionSec = followerBeat.t + beatPhase * followerBeatIntervalSec;

	const rawFollowerTempoRatio =
		(masterBeat.bpm * request.masterTempoRatio) / followerBeat.bpm;
	if (!Number.isFinite(rawFollowerTempoRatio) || rawFollowerTempoRatio <= 0) {
		throw new RangeError(`local PQTZ BPM produced invalid tempo ratio ${rawFollowerTempoRatio}`);
	}
	const tempo = _tempoRatioWithinRange(
		rawFollowerTempoRatio,
		request.minFollowerTempoRatio,
		request.maxFollowerTempoRatio
	);

	return {
		mode,
		syncAtContextTimeSec: request.syncAtContextTimeSec,
		masterPositionSec: projectedMasterPositionSec,
		masterBeatIndex,
		followerBeatIndex,
		masterBeatNumber,
		beatPhase,
		followerPositionSec,
		followerTempoRatio: tempo.ratio,
		tempoNormalization: tempo.normalization
	};
}
