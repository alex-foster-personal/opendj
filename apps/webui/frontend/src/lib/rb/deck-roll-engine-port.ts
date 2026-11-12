/** FXROLL-01: wire the audio engine's private helpers into a {@link DeckRollPort}. */
import type { DeckId } from '$lib/player/constants';
import type { DeckRollPort } from '$lib/rb/deck-roll-commands';
import type { DeckState, LoopState } from '$lib/rb/deck-state-types';
import type { AnlzBeat } from '$lib/rb/anlz-types';

export type { DeckRollPort };

export interface DeckRollEngineBind {
	deckStates: Record<DeckId, DeckState>;
	requireLoaded(deck: DeckId, op: string): { st: DeckState; rt: unknown };
	phaseGrid(st: DeckState, op: string): readonly AnlzBeat[];
	currentPosSec(deck: DeckId): number;
	getCtx(): AudioContext | null;
	futureScheduleTime(deck: DeckId): number | ((earliestContextTime: number) => number);
	scheduleDeck(
		deck: DeckId,
		when: number | ((earliestContextTime: number) => number),
		entry: number | ((effectiveWhen: number) => number),
		playing: boolean,
		a?: number,
		b?: boolean,
		loop?: LoopState | null
	): Promise<unknown>;
	slipHiddenPositionAt(rt: unknown, contextTime: number): number;
	clearSlip(deck: DeckId): void;
	resumeSlip(deck: DeckId): Promise<void>;
}

export function createDeckRollPortForAudioEngine(bind: DeckRollEngineBind): DeckRollPort {
	return {
		deckState: (deck) => bind.deckStates[deck],
		requireLoaded: bind.requireLoaded,
		phaseGrid: bind.phaseGrid,
		currentPosSec: bind.currentPosSec,
		scheduleDeckIntoLoop: async (deck, rt, loop) => {
			if (bind.getCtx() === null) throw new Error('roll: audio graph not initialised');
			await bind.scheduleDeck(
				deck,
				bind.futureScheduleTime(deck),
				(effectiveWhen) => bind.slipHiddenPositionAt(rt, effectiveWhen),
				true,
				undefined,
				undefined,
				loop
			);
		},
		clearSlip: bind.clearSlip,
		resumeSlip: bind.resumeSlip
	};
}
