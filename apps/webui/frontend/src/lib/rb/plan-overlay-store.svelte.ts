/** WAVEPLAN-7896: the pending plan's overlay for the waveform rows. The executor writes it; null with no plan pending. */
import type { PlanDeckOverlay, PlanOverlay } from './plan-overlay';

let current = $state.raw<PlanOverlay | null>(null);

export function setPlanOverlay(overlay: PlanOverlay | null): void {
	current = overlay;
}

export function planDeckOverlay(deck: number): PlanDeckOverlay | null {
	return current?.decks.find((row) => row.deck === deck) ?? null;
}

export function planAnchorMoved(): PlanOverlay['anchor_moved'] {
	return current?.anchor_moved ?? null;
}
