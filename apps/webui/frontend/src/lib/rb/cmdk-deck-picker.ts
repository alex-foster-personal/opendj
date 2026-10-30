/**
 * CMDK-09: the Cmd K deck picker's rules, runeless so the unit suite loads them.
 *
 * Order: the master deck first, then the rest by number. The master is listed
 * so the DJ sees what is on air, but it is locked: arrowing skips it and a
 * pick on it is refused, matching the browser's load path, which refuses a
 * master as the target of any load. Any other deck that is playing asks first
 * (the play-interrupt rule): only Cmd/Ctrl+Enter or a click on the confirm
 * button loads over it.
 */
import type { DeckId } from './deck-id';

export interface PickerDeck {
	id: DeckId;
	title: string | null;
	artist: string | null;
	loaded: boolean;
	playing: boolean;
	is_master: boolean;
}

/** Master first, then the others in deck order. */
export function pickerOrder(decks: readonly PickerDeck[]): PickerDeck[] {
	return [...decks].sort((a, b) => Number(b.is_master) - Number(a.is_master) || a.id - b.id);
}

/** Where the picker opens: on `target`, or on the first deck that is not the master. */
export function pickerStart(order: readonly PickerDeck[], target: DeckId): number {
	const at = order.findIndex((deck) => deck.id === target && !deck.is_master);
	if (at >= 0) return at;
	const free = order.findIndex((deck) => !deck.is_master);
	return free >= 0 ? free : 0;
}

/** One step up (dir -1) or down (dir 1), skipping the locked master; stays put at either end. */
export function pickerStep(order: readonly PickerDeck[], from: number, dir: 1 | -1): number {
	for (let i = from + dir; i >= 0 && i < order.length; i += dir) {
		if (!order[i].is_master) return i;
	}
	return from;
}

/** What picking `deck` does: refuse (the master), ask first (playing), or load. */
export function pickerAction(deck: PickerDeck, confirmed: boolean): 'refuse' | 'ask' | 'load' {
	if (deck.is_master) return 'refuse';
	if (deck.playing && !confirmed) return 'ask';
	return 'load';
}

/** The deck-state fields a replace guard compares: the loaded track and the engine's committed-load count. */
export interface GuardDeckState {
	stable_id: string | null;
	load_generation: number;
}

/** A replace guard: the playing deck it asked about and the load that was on it when it asked. */
export interface ReplaceGuard extends GuardDeckState {
	id: DeckId;
}

/** The guard for deck `id` as it stands now. */
export function replaceGuard(id: DeckId, state: GuardDeckState): ReplaceGuard {
	return { id, stable_id: state.stable_id, load_generation: state.load_generation };
}

/**
 * True while the deck still holds the load the guard asked about, so a confirm
 * answers the question that was shown. A newer load (another track, or the same
 * track loaded again by IPC, MIDI or AutoPlay) moves the generation, and the
 * confirm then must not replace a track nobody was asked about.
 */
export function guardHolds(guard: ReplaceGuard, state: GuardDeckState): boolean {
	return state.stable_id === guard.stable_id && state.load_generation === guard.load_generation;
}

/** WASD as arrows in the picker, which has no text to type; null for any other key. */
export function pickerArrow(event: {
	code: string;
	key: string;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
}): 'ArrowUp' | 'ArrowDown' | 'ArrowLeft' | 'ArrowRight' | null {
	if (event.key === 'ArrowUp' || event.key === 'ArrowDown' || event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
		return event.key;
	}
	if (event.metaKey || event.ctrlKey || event.altKey) return null;
	const wasd: Record<string, 'ArrowUp' | 'ArrowDown' | 'ArrowLeft' | 'ArrowRight'> = {
		KeyW: 'ArrowUp',
		KeyS: 'ArrowDown',
		KeyA: 'ArrowLeft',
		KeyD: 'ArrowRight'
	};
	return wasd[event.code] ?? null;
}
