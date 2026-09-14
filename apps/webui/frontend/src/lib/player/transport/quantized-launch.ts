/**
 * LATENCY-02: QUANTIZED LAUNCH planner (Class B, opt-in by modifier only).
 *
 * Pure math only: no DOM, no AudioContext, no engine imports.
 */

import type { AnlzBeat } from '$lib/rb/anlz-types';

export const QUANTIZED_LAUNCH = 'QUANTIZED LAUNCH';

type QuantizedLaunchPlan =
	| { kind: 'armed'; launchAtContextSec: number; followerStartSec: number }
	| { kind: 'refuse'; reason: string };

interface QuantizedLaunchInput {
	nowContextTimeSec: number;
	processorLeadSec: number;
	masterPlaying: boolean;
	masterBeats: readonly AnlzBeat[];
	masterPositionSec: number;
	masterTempoRatio: number;
	followerPlaying: boolean;
	followerBeats: readonly AnlzBeat[];
	followerPositionSec: number;
}

function _usableGrid(beats: readonly AnlzBeat[]): boolean {
	if (!Array.isArray(beats) || beats.length < 2) return false;
	let previous = Number.NEGATIVE_INFINITY;
	for (const beat of beats) {
		if (!Number.isInteger(beat.n) || beat.n < 1 || beat.n > 4) return false;
		if (!Number.isFinite(beat.t) || beat.t < previous) return false;
		previous = beat.t;
	}
	return true;
}

function _masterBeat1Candidates(beats: readonly AnlzBeat[], fromSec: number): readonly number[] {
	const downbeats = beats.filter((beat) => beat.n === 1);
	if (downbeats.length === 0) return [];
	const index = downbeats.findIndex((beat) => beat.t >= fromSec);
	if (index < 0) return [];
	return downbeats.slice(index).map((beat) => beat.t);
}

function _nextDownbeatSec(beats: readonly AnlzBeat[], positionSec: number): number | null {
	const downbeats = beats.filter((beat) => beat.n === 1);
	const found = downbeats.find((beat) => beat.t >= positionSec);
	return found === undefined ? null : found.t;
}

/**
 * Plan the next shared beat-1 launch instant for QUANTIZED LAUNCH.
 */
export function planQuantizedLaunch(input: QuantizedLaunchInput): QuantizedLaunchPlan {
	const refuse = (detail: string): QuantizedLaunchPlan => ({
		kind: 'refuse',
		reason: `${QUANTIZED_LAUNCH}: ${detail}`
	});
	if (input.followerPlaying) return refuse('follower is already playing');
	if (!input.masterPlaying) return refuse('no playing other-deck master');
	if (!_usableGrid(input.masterBeats) || !_usableGrid(input.followerBeats)) {
		return refuse('trusted beatgrid required on both decks');
	}
	if (!Number.isFinite(input.masterTempoRatio) || input.masterTempoRatio <= 0) {
		return refuse('master tempo ratio is invalid');
	}
	const candidates = _masterBeat1Candidates(input.masterBeats, input.masterPositionSec);
	if (candidates.length === 0) return refuse('master beatgrid has no future beat 1');
	const followerStartSec = _nextDownbeatSec(input.followerBeats, input.followerPositionSec);
	if (followerStartSec === null) {
		return refuse('follower beatgrid has no beat 1 in range');
	}
	for (const masterBeat1Sec of candidates) {
		const deltaTrackSec = masterBeat1Sec - input.masterPositionSec;
		if (deltaTrackSec < 0) continue;
		const launchAtContextSec = input.nowContextTimeSec + deltaTrackSec / input.masterTempoRatio;
		if (launchAtContextSec - input.nowContextTimeSec >= input.processorLeadSec) {
			return { kind: 'armed', launchAtContextSec, followerStartSec };
		}
	}
	return refuse('no master beat 1 lands outside the processor lead window');
}

interface QuantizedLaunchArmFacts {
	followerPlaying: boolean;
	followerDesiredActive: boolean;
	followerTrustedGrid: boolean;
	masterDeck: number | null;
	masterSameAsFollower: boolean;
	masterPlaying: boolean;
	masterTrustedGrid: boolean;
	nowContextTimeSec: number;
	processorLeadSec: number;
	masterBeats: readonly AnlzBeat[];
	masterPositionSec: number;
	masterTempoRatio: number;
	followerBeats: readonly AnlzBeat[];
	followerPositionSec: number;
}

/** Validate deck facts and plan the next QUANTIZED LAUNCH arm instant. */
export function computeQuantizedLaunchArm(
	facts: QuantizedLaunchArmFacts
): { launchAtContextSec: number; followerStartSec: number } {
	if (facts.followerDesiredActive || facts.followerPlaying) {
		throw new Error(`${QUANTIZED_LAUNCH}: follower is already playing`);
	}
	if (!facts.followerTrustedGrid) {
		throw new Error(`${QUANTIZED_LAUNCH}: follower lacks trusted beatgrid`);
	}
	if (facts.masterDeck === null || facts.masterSameAsFollower || !facts.masterPlaying || !facts.masterTrustedGrid) {
		throw new Error(`${QUANTIZED_LAUNCH}: no playing master`);
	}
	const plan = planQuantizedLaunch({
		nowContextTimeSec: facts.nowContextTimeSec,
		processorLeadSec: facts.processorLeadSec,
		masterPlaying: true,
		masterBeats: facts.masterBeats,
		masterPositionSec: facts.masterPositionSec,
		masterTempoRatio: facts.masterTempoRatio,
		followerPlaying: false,
		followerBeats: facts.followerBeats,
		followerPositionSec: facts.followerPositionSec
	});
	if (plan.kind === 'refuse') throw new Error(plan.reason);
	return { launchAtContextSec: plan.launchAtContextSec, followerStartSec: plan.followerStartSec };
}
