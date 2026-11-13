/**
 * How full a deck stem chip paints while its stem-set loads (STEMPERF).
 *
 * During a cloud `fetching` phase every chip shows the stem-set's file progress
 * (the engine does not name which file is which, so the fill is uniform). During
 * `downloading`, `decoding` and `switching` the fill is indeterminate: a sweep,
 * no invented number. Anything else paints no fill.
 */
import type { StemControl, StemDeckState } from '$lib/rb/stem-types';

export type StemChipFill = number | 'indeterminate' | null;

/** The fill (0..1, `indeterminate` or null) a stem chip paints for this stem-set state. */
export function stemChipFill(stems: StemDeckState, _stem: StemControl): StemChipFill {
	if (stems.status !== 'loading' || stems.load === null) return null;
	const { phase, progress } = stems.load;
	if (phase === 'downloading' || phase === 'decoding' || phase === 'switching')
		return 'indeterminate';
	if (phase !== 'fetching') return null;
	if (progress === null || !(progress.files_total > 0)) return 'indeterminate';
	const ratio = progress.files_done / progress.files_total;
	if (!Number.isFinite(ratio)) return 'indeterminate';
	return Math.min(1, Math.max(0, ratio));
}

/** The `data-fill` attribute value for a fill: the number, `indeterminate`, or absent. */
export function stemChipFillAttr(fill: StemChipFill): string | null {
	return fill === null ? null : String(fill);
}

/** True when a chip with this fill must stay disabled (any fill means the stem-set is not live). */
export function stemChipFillKeepsDisabled(fill: StemChipFill): boolean {
	return fill !== null;
}
