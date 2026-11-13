/**
 * STRUCT-9659: the library preview strip names the song section under the
 * cursor ("Breakdown", "Drop") while `show_sections` is on.
 *
 * Pure hover math, kept out of PreviewStrip.svelte so it is unit-tested at
 * the section edges: cursor x -> track time -> section (binary search over
 * the sorted spans from sections.ts `sectionSpans`), and the label's left
 * edge clamped so it never spills past either end of the strip. Hover never
 * repaints the canvas; the strip renders the result as a DOM label.
 */

/** The fields of a sections.ts `SectionSpan` the hover reads. */
export interface HoverSpan {
	start_s: number;
	end_s: number;
	label: string;
}

/** What the strip shows under the cursor: the section name and the label's left edge in CSS px. */
export interface SectionHoverLabel {
	label: string;
	leftPx: number;
}

/** Index of the span containing `tS` (start inclusive, end exclusive), or -1.
 * `spans` must be sorted by start and non-overlapping, as `sectionSpans` returns them. */
export function sectionIndexAt(spans: readonly HoverSpan[], tS: number): number {
	if (!Number.isFinite(tS)) return -1;
	let lo = 0;
	let hi = spans.length - 1;
	let found = -1;
	while (lo <= hi) {
		const mid = (lo + hi) >> 1;
		const span = spans[mid];
		if (span === undefined) break;
		if (span.start_s <= tS) {
			found = mid;
			lo = mid + 1;
		} else {
			hi = mid - 1;
		}
	}
	if (found < 0) return -1;
	const hit = spans[found];
	return hit !== undefined && tS < hit.end_s ? found : -1;
}

/** Left edge (CSS px) for a label of `labelWidthPx` centred on `cursorX`, clamped inside a strip of `widthPx`. */
export function clampLabelLeft(cursorX: number, labelWidthPx: number, widthPx: number): number {
	const width = Math.max(0, labelWidthPx);
	const maxLeft = Math.max(0, widthPx - width);
	return Math.min(maxLeft, Math.max(0, cursorX - width / 2));
}

/**
 * The section label for a hover at `cursorX` (CSS px from the strip's left
 * edge), or null when there is nothing to name: no hover, sections off, no
 * spans, no usable duration, or the cursor sits in a gap between sections.
 */
export function previewSectionHover(
	spans: readonly HoverSpan[] | null,
	cursorX: number | null,
	widthPx: number,
	durationS: number | null,
	labelWidthPx: number
): SectionHoverLabel | null {
	if (spans === null || spans.length === 0 || cursorX === null) return null;
	if (durationS === null || !Number.isFinite(durationS) || durationS <= 0 || widthPx <= 0)
		return null;
	const x = Math.min(widthPx, Math.max(0, cursorX));
	// The far right pixel maps to the last instant before the end, so a
	// section that runs to the end of the track still names itself there.
	const tS = Math.min((x / widthPx) * durationS, durationS - 1e-6);
	const i = sectionIndexAt(spans, tS);
	const span = i < 0 ? undefined : spans[i];
	if (span === undefined) return null;
	return { label: span.label, leftPx: clampLabelLeft(x, labelWidthPx, widthPx) };
}
