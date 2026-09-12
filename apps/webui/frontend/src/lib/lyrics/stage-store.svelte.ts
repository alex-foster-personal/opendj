/**
 * Stage view state - the ONE contract between every "open stage" button
 * (track page, deck header, context menu) and the overlay that renders it.
 *
 * The overlay is mounted once in the root layout; callers only ever touch
 * openStage/closeStage. `deck` distinguishes the two clock sources: null =
 * track-page mode (the overlay drives its own <audio> off /audio), a DeckId
 * = performance mode (the overlay follows that deck's engine clock).
 */

export type StageDeck = 1 | 2 | 3 | 4;

export interface StageState {
	open: boolean;
	stableId: string | null;
	deck: StageDeck | null;
}

export const stageState = $state<StageState>({ open: false, stableId: null, deck: null });

export function openStage(stableId: string, deck: StageDeck | null = null): void {
	if (!stableId) throw new Error('openStage: stableId is required');
	stageState.stableId = stableId;
	stageState.deck = deck;
	stageState.open = true;
}

export function closeStage(): void {
	stageState.open = false;
	// stableId/deck intentionally kept: reopening the same track is the
	// common case and keeps the overlay's loaded lyrics warm.
}
