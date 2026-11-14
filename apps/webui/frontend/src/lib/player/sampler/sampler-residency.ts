/**
 * Edge sampler: the residency plan (spec 5.1, 5.3, 6 audio, intent prefetch, closed panel).
 * Issue #10782 (S5). Pure: it decides which slots go hot (decoded) or warm (bytes) and in what
 * order, which slots are pinned, and when the pointer's path means the panel is about to open.
 * The clip pool (sampler-pool.ts) carries the plan out.
 *
 * - Budget: 32 / 64 / 128 MiB for the LOW / STANDARD / HIGH perf tier, the preview cache's tier
 *   table (perf-tier.ts) with its own figures, never its budget (spec 5.3).
 * - Pins: playing and armed voices, the preview, and slots pinned to a bank pad.
 * - Open panel: visible slots go hot in visibility order, one page either side goes warm;
 *   whatever is not touched ages out by LRU (scrolling away demotes).
 * - Intent: the pointer's path, extrapolated 120 ms ahead from its last 8 points, reaches the edge
 *   zone (mimicked: ForesightJS trajectory prediction defaults, `trajectoryPredictionTime` 120,
 *   `positionHistorySize` 8): the first visible page goes hot before the panel opens.
 * - Closed panel with no intent: an empty plan, so no fetch, decode, timer or rAF.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 the hot budget follows the perf tier and pins hold playing, armed, previewing and pad slots
 *     [if] the tier is LOW [then] the hot budget is 32 MiB [else ⛔️]
 *   ✔︎ ✅ 🎯 an open panel promotes visible slots hot in order, the pages either side warm
 *     [if] the panel is open [then] visible slots come first, hot, then neighbours, warm [else ⛔️]
 *   ✔︎ ✅ 🎯 a closed panel with no intent plans nothing
 *     [if] the panel is closed and the pointer is idle [then] the plan is empty [else ⛔️]
 *   ✔︎ ✅ 🎯 a fast move toward the edge reads as intent; a slow one or one away does not
 *     [if] the pointer will reach the edge zone within 120 ms [then] intent is true [else ⛔️]
 */

import type { PerfTierName } from '$lib/rb/perf-tier';
import type { VoiceState } from './sampler-types';

const MiB = 1024 * 1024;

/** Hot pool budget per perf tier (spec 5.3: LOW 32, default 64, HIGH 128 MiB). */
export const HOT_POOL_TIER_BYTES: Readonly<Record<PerfTierName, number>> = Object.freeze({
	LOW: 32 * MiB,
	STANDARD: 64 * MiB,
	HIGH: 128 * MiB
});

/** How far ahead the pointer's path is extrapolated (mimicked: ForesightJS default, ms). */
export const INTENT_LOOKAHEAD_MS = 120;
/** Pointer points the extrapolation reads (mimicked: ForesightJS positionHistorySize). */
export const INTENT_HISTORY_POINTS = 8;

/** The hot pool budget for a perf tier. */
export function hotPoolBudgetBytes(tier: PerfTierName): number {
	return HOT_POOL_TIER_BYTES[tier];
}

/** Slot ids the pool must never evict: sounding or armed voices, the preview, bank pad pins. */
export function residencyPins(
	voices: readonly Pick<VoiceState, 'slot_id' | 'phase'>[],
	previewSlotId: string | null,
	padPinned: Iterable<string>
): Set<string> {
	const pins = new Set(padPinned);
	for (const v of voices)
		if (v.slot_id !== null && (v.phase === 'armed' || v.phase === 'playing')) pins.add(v.slot_id);
	if (previewSlotId !== null) pins.add(previewSlotId);
	return pins;
}

export type Tier = 'hot' | 'warm';

/** What the panel shows: the visible page and one page either side, each in visibility order. */
export interface ResidencyView<S> {
	open: boolean;
	/** the pointer is heading for the edge (or the panel's shortcut was pressed) */
	intent: boolean;
	visible: readonly S[];
	neighbours: readonly S[];
}

/** The promotions to run, in order; empty for a closed panel with no intent. */
export function residencyPlan<S extends { slot_id: string }>(
	view: ResidencyView<S>
): { slot: S; tier: Tier }[] {
	if (!view.open && !view.intent) return [];
	const plan: { slot: S; tier: Tier }[] = [];
	const seen = new Set<string>();
	const add = (slots: readonly S[], tier: Tier): void => {
		for (const slot of slots) {
			if (seen.has(slot.slot_id)) continue;
			seen.add(slot.slot_id);
			plan.push({ slot, tier });
		}
	};
	add(view.visible, 'hot');
	if (view.open) add(view.neighbours, 'warm');
	return plan;
}

export interface PointerPoint {
	/** ms */
	t: number;
	/** px from the viewport's left */
	x: number;
}

/**
 * True when the pointer's path, its velocity across the last INTENT_HISTORY_POINTS points
 * extrapolated INTENT_LOOKAHEAD_MS ahead, lands inside the edge zone (`zonePx` from that edge).
 */
export function pointerIntent(
	points: readonly PointerPoint[],
	edge: 'L' | 'R',
	viewportWidth: number,
	zonePx: number
): boolean {
	const recent = points.slice(-INTENT_HISTORY_POINTS);
	if (recent.length < 2) return false;
	const first = recent[0];
	const last = recent[recent.length - 1];
	const dt = last.t - first.t;
	if (!(dt > 0)) return false;
	const vx = (last.x - first.x) / dt;
	const ahead = last.x + vx * INTENT_LOOKAHEAD_MS;
	if (edge === 'L') return vx < 0 && ahead <= zonePx;
	if (edge === 'R') return vx > 0 && ahead >= viewportWidth - zonePx;
	const _exhaustive: never = edge;
	throw new Error(`Unhandled edge: ${_exhaustive}`);
}
