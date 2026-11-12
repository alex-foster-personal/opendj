/**
 * Collapse and expand of a section in the track table, under the shared rule
 * in `$lib/motion/collapse.ts`: 100 ms, snapping above 200 rows or with reduced
 * motion, and nothing but transform and opacity animating.
 *
 * The table cannot use that module's grid wrapper: rows are virtualized `<tr>`s
 * with no block to wrap. So the mounted rows below the divider slide into their
 * new places, and rows being revealed fade in. Rows that are not mounted need
 * no animation, because nobody can see them.
 */
import { collapseDuration } from '$lib/motion/collapse';
import { prefersReducedMotion } from '$lib/motion/reduced-motion';
import type { SectionHeader } from './playlist-sections';

const EASE_OUT = 'cubic-bezier(0.33, 1, 0.68, 1)';

/** Pixels the rows below the divider travel: never more than one screen. */
export function sectionSlideDistance(params: {
	trackCount: number;
	rowHeight: number;
	viewportHeight: number;
}): number {
	const { trackCount, rowHeight, viewportHeight } = params;
	return Math.min(trackCount * rowHeight, Math.max(0, viewportHeight));
}

export function animateSectionToggle(
	tbody: HTMLElement,
	params: {
		header: SectionHeader;
		collapsing: boolean;
		rowHeight: number;
		viewportHeight: number;
	}
): void {
	const { header, collapsing, rowHeight, viewportHeight } = params;
	const duration = collapseDuration(header.trackCount, prefersReducedMotion());
	if (duration === 0) return;
	const divider = tbody.querySelector(`[data-section-id="${CSS.escape(header.section.id)}"]`);
	if (divider === null) return;
	const distance = sectionSlideDistance({
		trackCount: header.trackCount,
		rowHeight,
		viewportHeight
	});
	const options = { duration, easing: EASE_OUT };
	let line = divider.nextElementSibling;
	let revealed = collapsing ? 0 : header.trackCount;
	while (line !== null) {
		if (revealed > 0) {
			line.animate([{ opacity: 0 }, { opacity: 1 }], options);
			revealed -= 1;
		} else {
			const from = collapsing ? distance : -distance;
			line.animate([{ transform: `translateY(${from}px)` }, { transform: 'none' }], options);
		}
		line = line.nextElementSibling;
	}
}


