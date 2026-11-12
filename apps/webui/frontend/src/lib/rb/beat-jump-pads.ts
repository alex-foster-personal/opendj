/**
 * Beat Jump pad mode (DECKUX-9956): the eight performance pads as signed beat jumps.
 *
 * Mimics the rekordbox BEAT JUMP pad mode: the first four pads (A-D, the left column of the
 * 2x4 bank) jump back 1, 2, 4, 8 beats and the last four (E-H, the right column) jump forward
 * 1, 2, 4, 8 beats. Each pad sends the existing `beat_jump` command with the signed count shown
 * on it, so quantize, phase keeping and the end-of-track clamp are the engine's own behaviour.
 */

import type { DeckId } from './deck-id';

export interface BeatJumpPad {
	slot: 'A' | 'B' | 'C' | 'D' | 'E' | 'F' | 'G' | 'H';
	beats: number;
}

export const BEAT_JUMP_PADS: readonly BeatJumpPad[] = [
	{ slot: 'A', beats: -1 },
	{ slot: 'B', beats: -2 },
	{ slot: 'C', beats: -4 },
	{ slot: 'D', beats: -8 },
	{ slot: 'E', beats: 1 },
	{ slot: 'F', beats: 2 },
	{ slot: 'G', beats: 4 },
	{ slot: 'H', beats: 8 }
];

/** The label on a pad: the signed beat count it sends, e.g. `-4` or `+8`. */
export function beatJumpPadLabel(beats: number): string {
	return beats > 0 ? `+${beats}` : String(beats);
}

/** The typed performance command a pad press sends. */
export function beatJumpPadCommand(
	deck: DeckId,
	beats: number
): { type: 'beat_jump'; deck: DeckId; beats: number } {
	if (!Number.isInteger(beats) || beats === 0) {
		throw new RangeError(`beat jump pad beats must be a non-zero integer; got ${beats}`);
	}
	return { type: 'beat_jump', deck, beats };
}
