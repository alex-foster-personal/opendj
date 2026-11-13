/**
 * Selector node ids for a deck's transport and jog parts (#14331, epic #9372).
 *
 * `deck.<n>.transport` is a group holding one leaf per button (CUE, CALL <, CALL >, play).
 * `deck.<n>.jog` is a group holding the dial readout (the nudge target) and one leaf per
 * side button (Q, SLIP, MT, AU, MA). Each leaf wraps exactly one control, so Enter on a
 * leaf presses that button and no other. Ids compose under the existing deck part ids,
 * so selector-register.ts needs no new builder.
 */
import { selId } from '$lib/keyboard/selector-register';
import type { SelectorNodeKind } from '$lib/keyboard/selector-tree';
import type { DeckId } from '$lib/rb/deck-slots';

export const TRANSPORT_SEL_CONTROLS = ['cue', 'call-prev', 'call-next', 'play'] as const;
export type TransportSelControl = (typeof TRANSPORT_SEL_CONTROLS)[number];
export const JOG_SEL_CONTROLS = [
	'dial',
	'quantize',
	'slip',
	'master-tempo',
	'auto-cue',
	'manual-tempo'
] as const;
export type JogSelControl = (typeof JOG_SEL_CONTROLS)[number];

/** Kind of the deck's transport and jog parts: a group over their per-button leaves. */
export function deckTransportJogPartKind(): SelectorNodeKind {
	return 'group';
}

/** Leaf id of one transport button under the deck's transport group. */
export function transportSelId(deckId: DeckId, control: TransportSelControl): string {
	return `${selId.deckPart(deckId, 'transport')}.${control}`;
}

/** Leaf id of the jog dial readout or one jog side button under the deck's jog group. */
export function jogSelId(deckId: DeckId, control: JogSelControl): string {
	return `${selId.deckPart(deckId, 'jog')}.${control}`;
}
