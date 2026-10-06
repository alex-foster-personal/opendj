import type { HotCueMutation } from '$lib/rb/api-rb';
import { quantizeToNearestGridBeat } from '$lib/rb/beat-sync-math';
import type { DeckState } from '$lib/rb/deck-state-types';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import { deckHasTrustedBeatGrid, effectiveQuantize } from '$lib/player/grid-features';
import { dispatchPerformanceCommand, type PerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { pushToast } from '$lib/stores.svelte';

type DeckId = Extract<PerformanceCommand, { type: 'hot_cue_save' }>['deck'];

export interface DeckHotCueActions {
	saveHotCueAt: (slot: HotCueSlot, comment?: string, fixedPositionMs?: number, quantizeFixedPosition?: boolean, expectedStableId?: string) => Promise<HotCueMutation>;
	renameHotCueAt: (slot: HotCueSlot, inMs: number, comment: string) => Promise<HotCueMutation>;
	clearHotCueAt: (slot: HotCueSlot) => Promise<HotCueMutation>;
	restoreHotCueAt: (slot: HotCueSlot, revision: string, reversalId: string) => Promise<void>;
}

/**
 * Owns the deck-local persistence adapter shared by the UI and agent-native
 * IPC. Reading the deck lazily preserves the current revision after each
 * server round trip, which is mandatory for optimistic-concurrency writes.
 */
export function createDeckHotCueActions(
	readDeckId: () => DeckId,
	readDeck: () => DeckState
): DeckHotCueActions {
	async function saveHotCueAt(
		slot: HotCueSlot,
		comment?: string,
		fixedPositionMs?: number,
		quantizeFixedPosition = false,
		expectedStableId?: string
	): Promise<HotCueMutation> {
		const deck = readDeck();
		if (deck.stable_id === null) throw new Error(`hot cue ${slot}: deck is not loaded`);
		if (expectedStableId !== undefined && deck.stable_id !== expectedStableId) {
			throw new Error(`hot cue ${slot}: pending edit belongs to ${expectedStableId}, current track is ${deck.stable_id}`);
		}
		const revision = deck.hot_cue_revisions[slot];
		if (!revision) throw new Error(`hot cue ${slot}: slot revision is unavailable`);
		let ms = fixedPositionMs ?? deck.position_ms;
		const validPosition = Number.isFinite(ms) && ms >= 0;
		const beats = deck.anlz?.beatgrid.beats;
		if (validPosition && (fixedPositionMs === undefined || quantizeFixedPosition) && effectiveQuantize(deck) && beats !== undefined) {
			ms = Math.round(quantizeToNearestGridBeat(beats, ms / 1000, 1) * 1000); // off-grid positions are kept (SEEK-GRID-01)
		}
		if (Number.isFinite(ms) && ms >= 0) ms = Math.round(ms);
		try {
			const deckId = readDeckId();
			const command = {
				type: 'hot_cue_save' as const,
				deck: deckId,
				slot,
				in_ms: ms,
				revision
			};
			const state = await dispatchPerformanceCommand(
				comment === undefined ? command : { ...command, comment }
			);
			const reversal = state.decks[deckId].hot_cue_reversal;
			if (reversal === null) throw new Error(`hot cue ${slot}: dispatcher omitted reversal token`);
			return { cue: null, revision: reversal.revision, reversal: { reversal_id: reversal.reversal_id } };
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`Hot cue ${slot} save failed - ${message}`, 'error');
			throw error;
		}
	}

	async function renameHotCueAt(
		slot: HotCueSlot,
		inMs: number,
		comment: string
	): Promise<HotCueMutation> {
		return await saveHotCueAt(slot, comment, inMs);
	}

	async function clearHotCueAt(slot: HotCueSlot): Promise<HotCueMutation> {
		const deck = readDeck();
		if (deck.stable_id === null) throw new Error(`hot cue ${slot}: deck is not loaded`);
		const revision = deck.hot_cue_revisions[slot];
		if (!revision) throw new Error(`hot cue ${slot}: slot revision is unavailable`);
		try {
			const deckId = readDeckId();
			const state = await dispatchPerformanceCommand({
				type: 'hot_cue_clear', deck: deckId, slot, revision
			});
			const reversal = state.decks[deckId].hot_cue_reversal;
			if (reversal === null) throw new Error(`hot cue ${slot}: dispatcher omitted reversal token`);
			return { cue: null, revision: reversal.revision, reversal: { reversal_id: reversal.reversal_id } };
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`Hot cue ${slot} clear failed - ${message}`, 'error');
			throw error;
		}
	}

	async function restoreHotCueAt(
		slot: HotCueSlot,
		revision: string,
		reversalId: string
	): Promise<void> {
		const deck = readDeck();
		if (deck.stable_id === null) throw new Error(`hot cue ${slot}: deck is not loaded`);
		try {
			const deckId = readDeckId();
			await dispatchPerformanceCommand({
				type: 'hot_cue_restore', deck: deckId, slot, revision, reversal_id: reversalId
			});
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`Hot cue ${slot} restore failed - ${message}`, 'error');
			throw error;
		}
	}

	return { saveHotCueAt, renameHotCueAt, clearHotCueAt, restoreHotCueAt };
}
