/**
 * Selector node ids for a deck's pad area and beat jump (#13698, epic #9372).
 *
 * `deck.<n>.cues` is a group holding the grid edit buttons, the pad mode menu, the eight
 * performance pads (hot cue, beat jump or pad FX, whichever mode is mounted) and the hot
 * cue UNDO button. `deck.<n>.loop` is a group holding the loop cluster and the four beat
 * jump buttons. Ids are plain strings composed under the existing deck part ids, so
 * selector-register.ts needs no new builder.
 */
import { selId } from '$lib/keyboard/selector-register';
import type { DeckId } from '$lib/rb/deck-slots';

export const PAD_SLOT_LETTERS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'] as const;
export type PadSlotLetter = (typeof PAD_SLOT_LETTERS)[number];
export type GridEditSelPart = 'adjust' | 'shift';
export type BeatJumpSelControl = 'size-down' | 'size-up' | 'back' | 'forward';

const BEAT_JUMP_CONTROLS: readonly BeatJumpSelControl[] = [
	'size-down',
	'size-up',
	'back',
	'forward'
];
const GRID_EDIT_PARTS: readonly GridEditSelPart[] = ['adjust', 'shift'];

/** Leaf id of performance pad k (1 to 8, A to H) under the deck's cues group. */
export function deckPadSelId(deckId: DeckId, k: number): string {
	if (!Number.isInteger(k) || k < 1 || k > PAD_SLOT_LETTERS.length) {
		throw new Error(`Pad number must be an integer 1-8, got ${k}`);
	}
	return `${selId.deckPart(deckId, 'cues')}.pad${k}`;
}

/** Pad number 1 to 8 of a pad slot letter A to H. */
export function padNumberForSlot(slot: PadSlotLetter): number {
	return PAD_SLOT_LETTERS.indexOf(slot) + 1;
}

/** Leaf id of the pad mode menu trigger under the deck's cues group. */
export function padModeMenuSelId(deckId: DeckId): string {
	return `${selId.deckPart(deckId, 'cues')}.mode`;
}

/** Leaf id of a grid edit button (adjust or shift) under the deck's cues group. */
export function gridEditSelId(deckId: DeckId, part: GridEditSelPart): string {
	return `${selId.deckPart(deckId, 'cues')}.grid-${part}`;
}

/** Leaf id of the hot cue UNDO button under the deck's cues group. */
export function hotCueUndoSelId(deckId: DeckId): string {
	return `${selId.deckPart(deckId, 'cues')}.undo`;
}

/** Leaf id of the loop cluster under the deck's loop group. */
export function loopClusterSelId(deckId: DeckId): string {
	return `${selId.deckPart(deckId, 'loop')}.cluster`;
}

/** Leaf id of a beat jump button (size step or jump) under the deck's loop group. */
export function beatJumpSelId(deckId: DeckId, control: BeatJumpSelControl): string {
	return `${selId.deckPart(deckId, 'loop')}.jump-${control}`;
}

/** Every pad-area and beat jump id one deck registers, for the e2e reachability audit. */
export function deckPadSelectorIds(deckId: DeckId): string[] {
	return [
		...GRID_EDIT_PARTS.map((part) => gridEditSelId(deckId, part)),
		padModeMenuSelId(deckId),
		...PAD_SLOT_LETTERS.map((slot) => deckPadSelId(deckId, padNumberForSlot(slot))),
		hotCueUndoSelId(deckId),
		loopClusterSelId(deckId),
		...BEAT_JUMP_CONTROLS.map((control) => beatJumpSelId(deckId, control))
	];
}
