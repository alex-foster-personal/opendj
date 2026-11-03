/** WAVEPLAN-7896e: the words on a row's plan chip. Pure so a node test reads them without a browser. */
import type { PlanDeckOverlay, PlanOverlay } from './plan-overlay';

const UNAVAILABLE: Record<string, string> = {
	no_grid: 'this track has no beatgrid to place the curves on',
	no_tempo: 'a deck reports no tempo, so the incoming bars cannot be measured',
	track_changed: 'the deck no longer holds the track this plan was made for',
	build_failed: 'its curves could not be built'
};

function _signedBars(bars: number): string {
	return `${bars > 0 ? '+' : ''}${bars.toFixed(1)}`;
}

export function planChipText(overlay: PlanDeckOverlay, moved: PlanOverlay['anchor_moved']): string {
	if (overlay.unavailable !== null) return 'PLAN no curves';
	const span = overlay.role === 'clock' ? 'PLAN OUT' : 'PLAN IN';
	return overlay.role === 'incoming' && moved !== null ? `${span} moved ${_signedBars(moved.moved_bars)} bars` : span;
}

export function planChipTitle(overlay: PlanDeckOverlay, moved: PlanOverlay['anchor_moved']): string {
	if (overlay.unavailable !== null) return `Transition plan pending, but ${UNAVAILABLE[overlay.unavailable] ?? overlay.unavailable}.`;
	const curves = 'Solid line: fader. Dashed: EQ. Dotted: filter. Toward the row outer edge is 1, toward the far edge is 0; on a split row the outer edge is the top or bottom of the whole row.';
	if (overlay.role !== 'incoming' || moved === null) return `Transition plan pending. ${curves}`;
	return `This deck sits ${_signedBars(moved.moved_bars)} bars from bar ${moved.planned_bar}, the cue the plan was made for (now bar ${moved.live_bar}). The curves follow the deck and the plan still runs; plan again to fit the new cue. ${curves}`;
}
