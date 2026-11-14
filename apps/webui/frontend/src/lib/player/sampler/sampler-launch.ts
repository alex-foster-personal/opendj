/**
 * Edge sampler: pure launch, tempo, key, loop and playhead math (spec section 4.3, 4.4).
 * Issue #10781 (S4): the launch plan the voice applies; key lock (master tempo stretch) is the
 * default and vinyl (pitch follows rate) a per-slot option.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 a launch lands on the master's next quantise point, never in the past
 *     [if] quant is 'bar' [then] the start maps to a downbeat (n == 1) at least LAUNCH_LEAD_SEC away [else ⛔️]
 *     [if] quant is '4bars' [then] the start is every 4th downbeat counted from the grid's first downbeat [else ⛔️]
 *     [if] downbeat offset is +1 [then] the start moves one whole beat later [else ⛔️]
 *   ✔︎ ✅ 🎯 tempo match folds half/double time into [0.84, 1.16] when it can
 *     [if] a 70 BPM slot plays against 124 [then] the rate is 124/140 [else ⛔️]
 *   ✔︎ ✅ 🎯 key auto picks the smallest compatible shift (KEYSHIFT-02)
 */

import {
	COMPATIBLE_KEY_SHIFT_MAX,
	DEFAULT_CAMELOT_RULE_IDS,
	compatibleKeyShift,
	type CompatibleKeyRule
} from '$lib/player/key/camelot';
import type { KeyMode, LaunchQuant, TempoMode } from './sampler-types';
import { loopBeats, type Repeat } from './sampler-repeat';

/** Minimum gap between "now" and a scheduled start, so the start is never late. */
export const LAUNCH_LEAD_SEC = 0.05;
/** Lower edge of the half/double-time fold window for a matched rate. */
export const RATE_FOLD_MIN = 0.84;
/** Upper edge of the half/double-time fold window for a matched rate. */
export const RATE_FOLD_MAX = 1.16;

export interface GridBeat {
	/** beat within bar 1..4 */
	n: number;
	/** track seconds */
	t: number;
}

//----- launch quantise

function _candidateIndices(
	beats: readonly GridBeat[],
	quant: Exclude<LaunchQuant, 'off'>
): number[] {
	if (quant === 'beat') return beats.map((_, i) => i);
	const downbeats = beats.flatMap((b, i) => (b.n === 1 ? [i] : []));
	if (downbeats.length === 0) throw new Error('master beat grid has no downbeat');
	if (quant === 'bar') return downbeats;
	if (quant === '4bars') return downbeats.filter((_, k) => k % 4 === 0);
	const _exhaustive: never = quant;
	throw new Error(`Unhandled launch quant: ${_exhaustive}`);
}

/** AudioContext seconds of the next quantise point on the master, at least LAUNCH_LEAD_SEC from now. */
export function nextLaunchCtxSec(
	masterGridBeats: readonly GridBeat[],
	masterPosAtNowSec: number,
	nowCtxSec: number,
	tempoRatio: number,
	quant: LaunchQuant,
	downbeatOffsetBeats: number
): number {
	if (!Number.isFinite(nowCtxSec)) throw new Error(`nowCtxSec must be finite, got ${nowCtxSec}`);
	if (quant === 'off') return nowCtxSec + LAUNCH_LEAD_SEC;
	if (!(tempoRatio > 0)) throw new Error(`master tempoRatio must be > 0, got ${tempoRatio}`);
	if (!Number.isInteger(downbeatOffsetBeats))
		throw new Error(`downbeat offset must be whole beats, got ${downbeatOffsetBeats}`);
	if (masterGridBeats.length < 2) throw new Error('master beat grid is empty');
	const earliestPos = masterPosAtNowSec + LAUNCH_LEAD_SEC * tempoRatio;
	for (const i of _candidateIndices(masterGridBeats, quant)) {
		const target = masterGridBeats[i + downbeatOffsetBeats];
		if (target !== undefined && target.t >= earliestPos)
			return nowCtxSec + (target.t - masterPosAtNowSec) / tempoRatio;
	}
	throw new Error('master beat grid has no quantise point left after the playhead');
}

//----- tempo

/** Playback rate for a slot: own = 1; match = master/native folded by 2x into [0.84, 1.16] when possible. */
export function playbackRate(
	nativeBpm: number,
	masterBpm: number | null,
	tempo: TempoMode
): number {
	if (tempo === 'own' || masterBpm === null) return 1;
	if (tempo !== 'match') {
		const _exhaustive: never = tempo;
		throw new Error(`Unhandled tempo mode: ${_exhaustive}`);
	}
	if (!(nativeBpm > 0) || !(masterBpm > 0))
		throw new Error(`BPMs must be > 0, got native ${nativeBpm} master ${masterBpm}`);
	const raw = masterBpm / nativeBpm;
	const folds = [raw, raw / 2, raw * 2, raw / 4, raw * 4];
	return folds.reduce(
		(best, r) => (Math.abs(Math.log2(r)) < Math.abs(Math.log2(best)) ? r : best),
		raw
	);
}

/** True when a playback rate sits inside the half/double-time fold window. */
export function rateInFoldRange(rate: number): boolean {
	return rate >= RATE_FOLD_MIN && rate <= RATE_FOLD_MAX;
}

//----- key

export interface KeyPlan {
	/** semitones handed to the stretcher (or implied by a plain source when !masterTempo) */
	semitones: number;
	/** true = pitch independent of rate (stretcher); false = pitch follows rate (vinyl) */
	masterTempo: boolean;
}

/** The library's default compatible-key rule, so the sampler judges keys like the dropdown does. */
export const DEFAULT_KEY_RULE: CompatibleKeyRule = {
	mode: 'transition',
	rules: DEFAULT_CAMELOT_RULE_IDS
};

/** Semitones and stretch mode for a slot: auto shifts to the nearest compatible key, off is vinyl. */
export function keyPlan(
	slotKey: string | null,
	masterKey: string | null,
	mode: KeyMode,
	rate: number,
	rule: CompatibleKeyRule = DEFAULT_KEY_RULE
): KeyPlan {
	if (mode === 'off') return { semitones: 12 * Math.log2(rate), masterTempo: false };
	if (mode === 'master_tempo') return { semitones: 0, masterTempo: true };
	if (mode === 'auto') {
		const match = compatibleKeyShift(slotKey, masterKey, rule, COMPATIBLE_KEY_SHIFT_MAX);
		return { semitones: match === null ? 0 : match.shift, masterTempo: true };
	}
	const _exhaustive: never = mode;
	throw new Error(`Unhandled key mode: ${_exhaustive}`);
}

//----- loop window and playhead

/** Loop window in region seconds (0 = region in), at native rate. `regionSec` is the region length. */
export function loopWindow(
	regionBeats: number,
	repeat: Repeat,
	regionSec: number
): { loopStartSec: number; loopEndSec: number; loop: boolean } {
	if (!(regionBeats > 0) || !(regionSec > 0))
		throw new Error(`region must be > 0, got ${regionBeats} beats / ${regionSec} s`);
	const lb = loopBeats(repeat, regionBeats);
	if (lb === null) return { loopStartSec: 0, loopEndSec: regionSec, loop: false };
	return { loopStartSec: 0, loopEndSec: (regionSec * lb) / regionBeats, loop: true };
}

/** 0..1 position within the current pass; 0 before the start. */
export function playheadFraction(nowCtx: number, startCtx: number, passSec: number): number {
	if (!(passSec > 0)) throw new Error(`passSec must be > 0, got ${passSec}`);
	if (nowCtx <= startCtx) return 0;
	return ((nowCtx - startCtx) / passSec) % 1;
}
