/**
 * Memory cue CALL (rekordbox CALL < and >, mimicked): pick the previous or
 * next memory cue relative to the playhead. Pure; the engine moves the cue
 * point and playhead to the pick (`RbAudioEngine.callMemoryCue`), a paused
 * deck stays paused and a playing deck keeps playing from it.
 */
import type { AnlzCue } from './anlz-types';

export const MEMORY_CUE_CALL_DIRECTIONS = ['prev', 'next'] as const;
export type MemoryCueCallDirection = (typeof MEMORY_CUE_CALL_DIRECTIONS)[number];

export type MemoryCueCallPlan =
	{ kind: 'call'; cueMs: number } | { kind: 'refused'; reason: string };

/** Next: the first memory cue strictly after the playhead. Prev: the last
 * strictly before. None in that direction is a refusal with the reason. */
export function planMemoryCueCall(
	cues: readonly Pick<AnlzCue, 'kind' | 'in_ms'>[],
	positionMs: number,
	direction: MemoryCueCallDirection
): MemoryCueCallPlan {
	if (!Number.isFinite(positionMs))
		throw new RangeError(`memory_cue_call: positionMs must be finite, got ${String(positionMs)}`);
	if (!MEMORY_CUE_CALL_DIRECTIONS.includes(direction)) {
		throw new TypeError(
			`memory_cue_call: direction must be prev or next, got ${String(direction)}`
		);
	}
	const memoryMs = cues
		.filter((cue) => cue.kind === 'memory')
		.map((cue) => cue.in_ms)
		.sort((a, b) => a - b);
	if (memoryMs.length === 0) return { kind: 'refused', reason: 'this track has no memory cues' };
	const picked =
		direction === 'next'
			? memoryMs.find((ms) => ms > positionMs)
			: memoryMs.findLast((ms) => ms < positionMs);
	if (picked === undefined) {
		return {
			kind: 'refused',
			reason:
				direction === 'next'
					? 'no memory cue after the playhead'
					: 'no memory cue before the playhead'
		};
	}
	return { kind: 'call', cueMs: picked };
}
