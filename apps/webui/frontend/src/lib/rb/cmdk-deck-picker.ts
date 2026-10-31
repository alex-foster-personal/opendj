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

/**
 * A replace guard: the playing deck it asked about, the load that was on it when it asked,
 * how many loads had been dispatched to it then (a load still queued or decoding moves
 * that count before it moves the deck), and how many commands (play, pause, ...) had.
 */
export interface ReplaceGuard extends GuardDeckState {
	id: DeckId;
	loads: number;
	commands: number;
}

/** The guard for deck `id` as it stands now; `loads` and `commands` are the deck's dispatch counts. */
export function replaceGuard(id: DeckId, state: GuardDeckState, loads: number, commands: number): ReplaceGuard {
	return { id, stable_id: state.stable_id, load_generation: state.load_generation, loads, commands };
}

/**
 * True while the deck still holds the load the guard asked about, so a confirm
 * answers the question that was shown. A newer load (another track, or the same
 * track loaded again by IPC, MIDI or AutoPlay) moves the generation, and the
 * confirm then must not replace a track nobody was asked about. So does a load
 * dispatched but not yet landed, through the dispatch count (Codex r4277027005).
 * A command since the guard, such as a play that starts the deck, moves the command
 * count, so the confirm asks again rather than replace a track that now plays (Codex P1, #6689).
 */
export function guardHolds(guard: ReplaceGuard, state: GuardDeckState, loads: number, commands: number): boolean {
	return (
		state.stable_id === guard.stable_id &&
		state.load_generation === guard.load_generation &&
		loads === guard.loads &&
		commands === guard.commands
	);
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

/** What a cmdk_pick of a track reads on its deck: the picker's view of it, its queue and its load count. */
export interface PickGuardPorts {
	/** The deck as the picker sees it now, with its loaded track and generation; undefined when not listed. */
	deck(): (PickerDeck & GuardDeckState) | undefined;
	/** Loads dispatched to the deck (moves at dispatch, before the deck does). */
	loads(): number;
	/** Deck-queue commands other than loads dispatched to the deck: a play, seek or tempo sent during the wait voids a confirm. */
	commands(): number;
	/** Commands still queued for the deck. */
	pending(): number;
	/** Resolves true once the deck's queue drained, false when it did not settle. */
	idle(): Promise<boolean>;
}

/**
 * CMDK-19: the deck picker's guard for a cmdk_pick of a track, or why it refuses. What the
 * deck already has queued lands first, so the verdict reads the track really there, as the
 * picker's own confirm does. A confirm answers only for the load that was on the deck when the
 * pick came in: one that landed from the queue meanwhile is a track nobody confirmed, so the
 * pick is refused and asked again rather than replacing it (Codex r4278581453).
 *
 * `seen.loads` is set to the deck's load count the verdict was read against, so the caller can
 * refuse a load dispatched after it and before the pick starts (Codex r4278944613). `seen.commands`
 * is the same for every command: a seek or tempo sent after the read must win over the pick, not be
 * adopted as the pick's baseline (Codex BLOCKING, #6692).
 */
export async function pickGuard(
	id: DeckId,
	ports: PickGuardPorts,
	confirm: boolean,
	seen?: { loads: number; commands?: number }
): Promise<string | null> {
	const before = ports.deck();
	const asked = before === undefined ? null : replaceGuard(id, before, ports.loads(), ports.commands());
	if (ports.pending() > 0 && !(await ports.idle())) return `deck ${id} is still busy; pick again once it settles`;
	const now = ports.deck();
	if (seen !== undefined) {
		seen.loads = ports.loads();
		seen.commands = ports.commands();
	}
	if (now === undefined) return null;
	const verdict = pickerAction(now, confirm);
	if (verdict === 'refuse') return `deck ${id} is the master; pick another deck`;
	if (verdict === 'ask') return `deck ${id} is playing; pick again with confirm to replace it`;
	// A confirm covers the load it was sent against, playing or paused: any other load that lands
	// on the deck before the pick is refused, a paused one from the queue included (CMDK-19, Sol P1).
	// A pick without confirm has no guard, so a paused deck still loads straight through.
	if (confirm && (asked === null || !guardHolds(asked, now, ports.loads(), ports.commands()))) {
		return `deck ${id} took another load before this pick; pick again with confirm to replace it`;
	}
	return null;
}
