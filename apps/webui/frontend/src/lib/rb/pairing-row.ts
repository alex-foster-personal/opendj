/** PAIR-04 and PAIR-09 library chrome: which rows draw as paired, and how. */

import { MASTER_FOLD_MIN_LEFT_PX } from './master-fold-anchor';

/** True when `stableId` is a pairing partner of the current master. TrackTable
 * binds this to the row's `rb-row-paired` class, fed by PairingIndex.partnerIds,
 * and the PAIR pointer targets the nearest such row off screen. */
export function isPairedRow(partnerIds: ReadonlySet<string>, stableId: string): boolean {
	return partnerIds.has(stableId);
}

/** The two ends of a pairing; `Pairing` from the API satisfies it. */
export interface PairingEnds {
	from_stable_id: string;
	to_stable_id: string;
}

/** Every track's partners, both directions. A pairing is directional data,
 * but "is this row paired" and "is its partner loaded" look both ways. */
export function buildPartnerMap(pairings: Iterable<PairingEnds>): Map<string, Set<string>> {
	const map = new Map<string, Set<string>>();
	const link = (a: string, b: string): void => {
		let set = map.get(a);
		if (set === undefined) {
			set = new Set();
			map.set(a, set);
		}
		set.add(b);
	};
	for (const p of pairings) {
		if (p.from_stable_id === p.to_stable_id) continue;
		link(p.from_stable_id, p.to_stable_id);
		link(p.to_stable_id, p.from_stable_id);
	}
	return map;
}

/**
 * How a library row draws its pairing (PAIR-09, the maintainer Thu 8 Oct 2026):
 * - `none`: not paired with anything.
 * - `still`: paired, partner not on a deck - a static purple zigzag edge.
 * - `live`: paired and a partner is loaded on a deck - the zigzag crackles.
 * Gating the motion on a loaded partner keeps a long library calm and makes
 * the animation mean something when it shows.
 */
export type PairRowState = 'none' | 'still' | 'live';

export function pairRowState(
	partners: ReadonlyMap<string, ReadonlySet<string>>,
	loadedIds: ReadonlySet<string>,
	stableId: string
): PairRowState {
	const mine = partners.get(stableId);
	if (mine === undefined || mine.size === 0) return 'none';
	for (const id of mine) if (loadedIds.has(id)) return 'live';
	return 'still';
}

/** Table-order indices of every row that is a partner of the master. A
 * master can have several partners, so the PAIR pointer weighs all of them. */
export function partnerIndices(
	rows: ReadonlyArray<{ stable_id: string }>,
	partnerIds: ReadonlySet<string>
): number[] {
	if (partnerIds.size === 0) return [];
	const out: number[] = [];
	rows.forEach((r, i) => {
		if (partnerIds.has(r.stable_id)) out.push(i);
	});
	return out;
}

/**
 * Where the PAIR pointer should point, or null when it should not show.
 *
 * Any partner row on screen hides it: a partner is already in view. With
 * every partner off screen it points at the NEAREST one (by row distance
 * from `centerRow`), above or below, and that row is where a click jumps.
 * `fold(i)` is the visibility of row i, 'above' | 'below' | null.
 */
export function nearestOffscreenPartner(
	indices: readonly number[],
	fold: (rowIndex: number) => 'above' | 'below' | null,
	centerRow: number
): { index: number; fold: 'above' | 'below' } | null {
	let best: { index: number; fold: 'above' | 'below' } | null = null;
	for (const i of indices) {
		const f = fold(i);
		if (f === null) return null;
		if (best === null || Math.abs(i - centerRow) < Math.abs(best.index - centerRow)) {
			best = { index: i, fold: f };
		}
	}
	return best;
}

/** Gap between the MASTER badge and the PAIR arrow when both sit on one edge. */
export const PAIR_FOLD_GAP_PX = 6;

/** Centre-to-centre distance from MASTER's centre to the PAIR arrow's centre when both share an edge. */
export function pairFoldOffsetPx(masterWidthPx: number, pairWidthPx: number): number {
	return masterWidthPx / 2 + PAIR_FOLD_GAP_PX + pairWidthPx / 2;
}

/**
 * Horizontal centre of the PAIR arrow. Alone on its edge it takes the same
 * centre as the MASTER badge would (over the title column). When MASTER is on
 * the same edge, the arrow sits 6px to its right (PAIR-09), so the two never
 * overlap. Keeping that relation near the wrap's right edge is the job of
 * `pairFoldGroupMasterCenterPx`, which moves MASTER, not the arrow.
 */
export function pairFoldCenterPx(params: {
	titleCenterPx: number;
	masterOnSameEdge: boolean;
	masterWidthPx: number;
	pairWidthPx: number;
}): number {
	const { titleCenterPx, masterOnSameEdge, masterWidthPx, pairWidthPx } = params;
	if (!masterOnSameEdge) return titleCenterPx;
	return titleCenterPx + pairFoldOffsetPx(masterWidthPx, pairWidthPx);
}

/**
 * MASTER's centre when it shares an edge with the PAIR arrow. The two clamp as
 * one group: if the arrow would run past the wrap's right edge, MASTER shifts
 * left by just enough to keep the arrow inside, so the arrow stays 6px to
 * MASTER's right. `masterCenterPx` is MASTER's own clamped centre; the result
 * never goes below MASTER's left floor, so a very narrow wrap keeps MASTER on
 * screen and lets the arrow overflow instead.
 */
export function pairFoldGroupMasterCenterPx(params: {
	masterCenterPx: number;
	masterWidthPx: number;
	pairWidthPx: number;
	wrapWidthPx: number;
}): number {
	const { masterCenterPx, masterWidthPx, pairWidthPx, wrapWidthPx } = params;
	const maxCentre =
		wrapWidthPx - MASTER_FOLD_MIN_LEFT_PX - pairWidthPx / 2 - pairFoldOffsetPx(masterWidthPx, pairWidthPx);
	const minCentre = MASTER_FOLD_MIN_LEFT_PX + masterWidthPx / 2;
	return Math.max(minCentre, Math.min(masterCenterPx, maxCentre));
}

/**
 * Half-width to clamp the PAIR arrow's title centre by. Beside the MASTER
 * badge it must be the BADGE's half-width, so the arrow starts from the
 * badge's own clamped centre: clamped by the narrower arrow instead, a
 * title scrolled off to the left pulls the arrow back under the badge.
 */
export function pairFoldClampHalfWidthPx(params: {
	masterOnSameEdge: boolean;
	masterWidthPx: number;
	pairWidthPx: number;
}): number {
	return (params.masterOnSameEdge ? params.masterWidthPx : params.pairWidthPx) / 2;
}
