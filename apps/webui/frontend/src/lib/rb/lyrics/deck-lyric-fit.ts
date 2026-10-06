/**
 * Fit the deck lyric column's CURRENT line into the box it actually has
 * (LYR-10, the maintainer's bug B3: "lyrics try to have a 3rd line of words but they
 * just disappear"). The column beside the hot cues is ~115px wide, so a
 * real line of 100+ characters never fits one row; it used to be clipped to
 * one ellipsized row and most of its words vanished.
 *
 * The rule, in priority order, and it NEVER drops a word:
 *  1. wrap the whole line at the normal size, keeping every preview row;
 *  2. shrink the font in steps down to the floor, still keeping previews;
 *  3. give the preview rows' height to the current line, one row at a time,
 *     and retry 1-2 (the sung line matters more than what comes next);
 *  4. if even the floor size with no previews cannot hold the whole line,
 *     split it into consecutive PAGES that each fit, shown one after another
 *     as the line plays. Every word still appears.
 *
 * Pure: the caller supplies `measure`, which lays the text out at a given
 * font size in the column's real width and reports its height. The font only
 * changes when the line or the box changes, never per frame, so the layout
 * does not jump while a line is sung.
 *
 * Requirements (mini-PRD):
 *   ✔︎ fitDeckLyricLine keeps the base size and every preview for a line that fits.
 *     [if] a short line comes back smaller than baseFontPx [then ⛔️]
 *   ✔︎ a long line wraps and shrinks but stays one page while that can hold it.
 *     [if] the real 103-char reproducer is split or clipped in a 115x90 box [then ⛔️]
 *   ✔︎ every word lands on exactly one page, in order, even in a 1-row box.
 *     [if] the concatenated pages differ from the input words [then ⛔️]
 */

export interface DeckLyricFitOptions {
	/** The normal size: what a line that fits is drawn at. */
	baseFontPx: number;
	/** Never shrink below this; page instead. */
	floorFontPx: number;
	stepPx: number;
	/** Preview rows the row budget allows (0-2). */
	maxPreviews: number;
	/** Fixed height of one preview row. */
	previewRowPx: number;
	/** Inner height of the whole lyric column. */
	boxHeightPx: number;
}

export interface DeckLyricFit {
	fontPx: number;
	/** Preview rows still shown under the current line. */
	previews: number;
	/** Start word index of each page; [0] when the line fits whole. */
	pageStarts: number[];
}

/** Height the text needs at `fontPx` in the column's width; `Infinity`
 *  when a single word is wider than the column at that size. */
export type MeasureLyricText = (text: string, fontPx: number) => number;

function _fontSteps(options: DeckLyricFitOptions): number[] {
	const steps: number[] = [];
	for (let px = options.baseFontPx; px >= options.floorFontPx - 1e-9; px -= options.stepPx) {
		steps.push(Math.round(px * 100) / 100);
	}
	if (steps.length === 0) steps.push(options.floorFontPx);
	return steps;
}

function _pageAt(
	words: readonly string[],
	fontPx: number,
	availablePx: number,
	measure: MeasureLyricText
): number[] {
	const starts: number[] = [0];
	let start = 0;
	for (let end = 1; end < words.length; end += 1) {
		const candidate = words.slice(start, end + 1).join(' ');
		if (measure(candidate, fontPx) > availablePx) {
			starts.push(end);
			start = end;
		}
	}
	return starts;
}

export function fitDeckLyricLine(
	words: readonly string[],
	options: DeckLyricFitOptions,
	measure: MeasureLyricText
): DeckLyricFit {
	const unmeasured: DeckLyricFit = {
		fontPx: options.baseFontPx,
		previews: options.maxPreviews,
		pageStarts: [0]
	};
	if (words.length === 0 || !(options.boxHeightPx > 0)) return unmeasured;
	const text = words.join(' ');
	const fonts = _fontSteps(options);
	for (let previews = options.maxPreviews; previews >= 0; previews -= 1) {
		const available = options.boxHeightPx - previews * options.previewRowPx;
		if (available <= 0) continue;
		for (const fontPx of fonts) {
			if (measure(text, fontPx) <= available) return { fontPx, previews, pageStarts: [0] };
		}
	}
	return {
		fontPx: options.floorFontPx,
		previews: 0,
		pageStarts: _pageAt(words, options.floorFontPx, options.boxHeightPx, measure)
	};
}

/** Which page is on screen: the one holding `wordIndex` (relative to the
 *  line) when a word is live, otherwise the share of the line already
 *  played. Clamped, so a stray progress value never selects no page. */
export function deckLyricPageIndex(
	pageStarts: readonly number[],
	wordInLine: number | null,
	lineProgress: number
): number {
	if (pageStarts.length <= 1) return 0;
	if (wordInLine !== null && wordInLine >= 0) {
		let page = 0;
		for (let i = 0; i < pageStarts.length; i += 1) if (pageStarts[i] <= wordInLine) page = i;
		return page;
	}
	const progress = Number.isFinite(lineProgress) ? Math.min(Math.max(lineProgress, 0), 1) : 0;
	return Math.min(pageStarts.length - 1, Math.floor(progress * pageStarts.length));
}
