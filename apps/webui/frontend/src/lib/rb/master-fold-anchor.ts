/**
 * Where the "▲ MASTER" / "▼ MASTER" jump badge sits over the track table.
 *
 * Pin b44c957f082f (the maintainer, Wed 2 Sep 2026): "jump to master button should be
 * aligned with track title col. lazy updates when window resized to minimize
 * rendering cost (requirement)."
 *
 * It was `left: 50%` - the middle of the table, which lands over Rating /
 * Comments and points at nothing. The badge is about a track, and the track's
 * name is in the title column, so that is what it should sit over.
 *
 * The second half of the pin is answered by construction rather than by a
 * debounce: this is a sum over the column widths the table already holds as
 * state, plus the scroll offset its wrap already reports. Nothing here
 * measures the window, so a window resize recomputes nothing and there is no
 * listener to make lazy. That is cheaper than a lazy update and it cannot
 * drift out of sync with one.
 */

/** Columns rendered before the title, in order. Kept as data rather than a
 * hardcoded sum so a new column between them cannot silently desync the
 * badge from the header it is pointing at. */
export const MASTER_FOLD_COLS_BEFORE_TITLE = [
	'funnel',
	'err',
	'cloud',
	'order',
	'preview',
	'art'
] as const;

/** Keep the badge clickable when the title column is scrolled off to the
 * left: it stops here rather than following the column out of the panel. */
export const MASTER_FOLD_MIN_LEFT_PX = 12;

/**
 * Horizontal centre for the badge, in pixels from the table wrap's left edge.
 * Pair it with `transform: translateX(-50%)`.
 *
 * `wrapWidth` is the wrap's own rendered width (already state the caller's
 * `ResizeObserver` holds for `viewportHeight` - this just also reads its
 * width). Without an upper clamp, a title column scrolled past a narrow wrap
 * pushes the badge off the wrap's own RIGHT edge exactly as the min clamp
 * guards the left. `undefined` skips the max clamp, matching every existing
 * caller and test that never measured a width.
 *
 * `badgeHalfWidthPx` is half the badge's own rendered width (Sol review
 * r3941617671): the clamp bounds a CENTRE, but `translateX(-50%)` means the
 * badge's actual edge sits `badgeHalfWidthPx` further out than that centre in
 * both directions, so clamping the centre alone still lets roughly half the
 * button hang off either edge. Defaults to 0 (the pre-fix behaviour) so every
 * existing caller and test that never measured the badge keeps clamping the
 * centre exactly at the wrap edge, same as before.
 */
export function masterFoldCenterPx(
	widths: Record<string, number>,
	scrollLeft: number,
	wrapWidth?: number,
	badgeHalfWidthPx = 0
): number {
	let left = 0;
	for (const col of MASTER_FOLD_COLS_BEFORE_TITLE) {
		left += widths[col] ?? 0;
	}
	const centre = left + (widths.title ?? 0) / 2 - scrollLeft;
	const clampedMin = Math.max(MASTER_FOLD_MIN_LEFT_PX + badgeHalfWidthPx, centre);
	if (wrapWidth === undefined) return clampedMin;
	return Math.min(clampedMin, wrapWidth - MASTER_FOLD_MIN_LEFT_PX - badgeHalfWidthPx);
}
