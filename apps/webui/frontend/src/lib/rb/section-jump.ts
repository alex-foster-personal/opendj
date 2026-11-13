/**
 * Section jump (issue #10263): one press takes a deck to the next or previous
 * analysed section start, landed on the beatgrid's nearest downbeat.
 *
 * Pure: the engine turns a `seek` plan into its quantize-aware seek. A deck
 * with no phrases is refused with `NO_SECTIONS` and moves nowhere.
 *
 * - `next` picks the first section start after the position.
 * - `previous` picks the last section start more than SECTION_JUMP_GRACE_SEC
 *   before the position, so a repeated "previous" steps back a section
 *   instead of re-landing on the one just reached. Inside the grace window
 *   of the first section it lands on that first section.
 * - Starts are snapped to the nearest downbeat (`n === 1`) BEFORE the pick,
 *   so a start that snaps behind the position is never a forward target.
 *   With no downbeats the raw starts are used.
 */
import type { AnlzBeat, AnlzPhrase } from '$lib/rb/anlz-types';

export type SectionJumpDirection = 'next' | 'previous';
export const SECTION_JUMP_DIRECTIONS: readonly SectionJumpDirection[] = ['next', 'previous'];
/** A "previous" this close after a section start goes back one more section. */
export const SECTION_JUMP_GRACE_SEC = 1;

export type SectionJumpRefusal = 'NO_SECTIONS' | 'NO_SECTION_AHEAD' | 'NO_SECTION_BEHIND';

export type SectionJumpPlan =
	{ kind: 'seek'; targetMs: number } | { kind: 'refused'; reason: SectionJumpRefusal };

export interface SectionJumpInput {
	/** The deck's projected playhead, in seconds. */
	positionSec: number;
	direction: SectionJumpDirection;
	phrases: readonly AnlzPhrase[];
	beats: readonly AnlzBeat[];
}

/** Times of the grid's downbeats, in order. */
export function downbeatTimesSec(beats: readonly AnlzBeat[]): number[] {
	const out: number[] = [];
	for (const beat of beats) if (beat.n === 1) out.push(beat.t);
	return out;
}

/** The downbeat nearest `t`; `t` itself when the grid has no downbeats. */
export function nearestDownbeatSec(downbeatsSec: readonly number[], t: number): number {
	if (downbeatsSec.length === 0) return t;
	let lo = 0;
	let hi = downbeatsSec.length - 1;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (downbeatsSec[mid] < t) lo = mid + 1;
		else hi = mid;
	}
	if (lo > 0 && t - downbeatsSec[lo - 1] <= downbeatsSec[lo] - t) return downbeatsSec[lo - 1];
	return downbeatsSec[lo];
}

/** Section starts snapped onto the grid's downbeats, sorted and deduplicated. */
export function snappedSectionStartsSec(
	sectionStartsSec: readonly number[],
	downbeatsSec: readonly number[]
): number[] {
	const snapped = sectionStartsSec
		.filter((s) => Number.isFinite(s))
		.map((s) => nearestDownbeatSec(downbeatsSec, s));
	return [...new Set(snapped)].sort((a, b) => a - b);
}

/**
 * The section start (in seconds, on a downbeat) a jump from `positionSec`
 * lands on, or null when nothing lies in that direction.
 */
export function sectionJumpTarget(
	positionSec: number,
	direction: SectionJumpDirection,
	sectionStartsSec: readonly number[],
	downbeatsSec: readonly number[] = []
): number | null {
	if (!Number.isFinite(positionSec) || positionSec < 0)
		throw new RangeError(`positionSec must be finite and >= 0; got ${positionSec}`);
	const starts = snappedSectionStartsSec(sectionStartsSec, downbeatsSec);
	if (starts.length === 0) return null;
	if (direction === 'next') {
		return starts.find((s) => s > positionSec) ?? null;
	} else if (direction === 'previous') {
		const behind = starts.filter((s) => s < positionSec - SECTION_JUMP_GRACE_SEC);
		if (behind.length > 0) return behind[behind.length - 1];
		return positionSec > starts[0] ? starts[0] : null;
	}
	throw new RangeError(`direction must be next or previous; got ${String(direction)}`);
}

export function planSectionJump(input: SectionJumpInput): SectionJumpPlan {
	if (input.phrases.length === 0) return { kind: 'refused', reason: 'NO_SECTIONS' };
	const target = sectionJumpTarget(
		input.positionSec,
		input.direction,
		input.phrases.map((phrase) => phrase.start_s),
		downbeatTimesSec(input.beats)
	);
	if (target === null) {
		return {
			kind: 'refused',
			reason: input.direction === 'next' ? 'NO_SECTION_AHEAD' : 'NO_SECTION_BEHIND'
		};
	}
	return { kind: 'seek', targetMs: Math.round(target * 1000) };
}
