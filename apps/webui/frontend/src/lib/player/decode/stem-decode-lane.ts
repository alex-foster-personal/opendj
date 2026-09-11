/**
 * Runtime lane trial for stem decode: which lane is faster on THIS engine.
 *
 * Extracted from flac-stem-decode.ts so the policy file can grow MPEG
 * eligibility without crossing the 600-line ratchet. Trials are keyed by
 * `` `${codec}:${width}` `` so a FLAC demucs4 verdict cannot pin an MPEG
 * demucs4 load.
 */

export type DecodeLane = 'workers' | 'main-thread';
export type StemDecodeCodec = 'flac' | 'mpeg';

/** How much faster the workers must be to win, so noise cannot flip the choice. */
export const LANE_MARGIN = 1.25;

function _trialKey(width: number, codec: StemDecodeCodec): string {
	return `${codec}:${width}`;
}

/**
 * Encoded bytes per wall millisecond, per lane, until one wins - PER CODEC
 * AND LAYOUT.
 */
const _laneTrials = new Map<string, Map<DecodeLane, number>>();
const _laneVerdicts = new Map<string, DecodeLane>();
/** A lane in force for EVERY layout and codec: set by `forceLane`. */
let _contextLane: DecodeLane | null = null;

function _settledLane(width: number, codec: StemDecodeCodec): DecodeLane | null {
	return _contextLane ?? _laneVerdicts.get(_trialKey(width, codec)) ?? null;
}

/** The lane being trialed right now, or null when no trial is in flight. */
let _trialLane: DecodeLane | null = null;
/** Set when any other load overlapped the trial in flight. */
let _trialContended = false;
/** Loads decoding right now, trial or not. */
let _loadsInFlight = 0;

/** Which lane a load runs, and whether its wall time is a measurement. */
export interface LaneClaim {
	lane: DecodeLane;
	trialing: boolean;
	/** Part count: the layout whose trials this load reads and feeds. */
	width: number;
	codec: StemDecodeCodec;
}

/**
 * Claim this load's lane, SYNCHRONOUSLY, before the function's first await.
 */
export function claimLane(
	width: number,
	everyPartIsEligible: boolean,
	codec: StemDecodeCodec
): LaneClaim {
	_loadsInFlight += 1;
	if (_trialLane !== null) _trialContended = true;
	const settled = _settledLane(width, codec);
	if (settled !== null) return { lane: settled, trialing: false, width, codec };
	if (_trialLane !== null || _loadsInFlight > 1 || !everyPartIsEligible) {
		return { lane: 'main-thread', trialing: false, width, codec };
	}
	const key = _trialKey(width, codec);
	const lane: DecodeLane = _laneTrials.get(key)?.has('main-thread') ? 'workers' : 'main-thread';
	_trialLane = lane;
	_trialContended = false;
	return { lane, trialing: true, width, codec };
}

/** Give the claim back, recording the trial only if it stayed measurable. */
export function releaseLane(claim: LaneClaim, clean: boolean, bytes: number, wallMs: number): void {
	_loadsInFlight -= 1;
	if (!claim.trialing) return;
	const contended = _trialContended;
	_trialLane = null;
	_trialContended = false;
	if (clean && !contended) recordTrial(claim.width, claim.codec, claim.lane, bytes, wallMs);
}

function recordTrial(
	width: number,
	codec: StemDecodeCodec,
	lane: DecodeLane,
	bytes: number,
	wallMs: number
): void {
	if (_settledLane(width, codec) !== null || bytes <= 0 || wallMs <= 0) return;
	const key = _trialKey(width, codec);
	const trials = _laneTrials.get(key) ?? new Map<DecodeLane, number>();
	_laneTrials.set(key, trials);
	trials.set(lane, bytes / wallMs);
	const main = trials.get('main-thread');
	const workers = trials.get('workers');
	if (main === undefined || workers === undefined) return;
	_laneVerdicts.set(key, workers > main * LANE_MARGIN ? 'workers' : 'main-thread');
}

/** Settle the lane without running the two calibration loads. */
export function forceLane(lane: DecodeLane): void {
	_contextLane = lane;
	_laneVerdicts.clear();
	_laneTrials.clear();
	_trialLane = null;
	_trialContended = false;
}

/** Forget the verdict and both trials, reservation included. */
export function resetLane(): void {
	_contextLane = null;
	_laneVerdicts.clear();
	_laneTrials.clear();
	_trialLane = null;
	_trialContended = false;
}

/** The lane in force for a layout of `width` parts and `codec`, or null while unmeasured. */
export function settledLane(width: number, codec: StemDecodeCodec = 'flac'): DecodeLane | null {
	return _settledLane(width, codec);
}

export function trialing(): boolean {
	return _trialLane !== null;
}

export function loadsInFlight(): number {
	return _loadsInFlight;
}

/** Pin the session to main-thread when a rate the workers cannot serve is discovered. */
export function settleContextOnMainThread(): void {
	_contextLane = 'main-thread';
}
