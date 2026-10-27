/**
 * Auto-fit for the library Key and BPM columns, measured in the active skin's
 * font (the maintainer, Tue 27 Oct 2026: Gothic ellipsized both columns).
 *
 * The old sizing assumed 7px per character and capped Key at 36px. Gothic and
 * Light render the table in JetBrains Mono at 10.5px, whose advance is 0.6em
 * (6.3px), and the default key notation is now `8A • Am`, so every guess was
 * short. This module takes a text measurer instead of a per-character guess:
 * the table passes a canvas `measureText` bound to the font the table really
 * renders in, and tests pass a measurer built from the font's real metrics.
 *
 * A column fits the widest of: every key or BPM string its rows print, the
 * status words "missing" and "failed", and its header (master value, bold,
 * plus the sort arrow, which is counted even when unsorted so sorting never
 * reflows the column). Cells clip, never ellipsize, so the only failure mode
 * left is a measurer that under-reads, which the per-skin tests pin.
 *
 * Requirements (mini-PRD):
 *   ? fitMusicalColumns: each width >= every candidate text's measured width + 2 * padX.
 *     [if] the Gothic Key column is narrower than "12A • Dbm" in 10.5px JetBrains Mono [then ⛔️]
 *     [if] the BPM column is narrower than "missing" in the skin font [then ⛔️]
 *     [if] a header master value wider than every cell is cut off [then ⛔️]
 */
import { bpmCellText } from './bpm-cell-text';
import { displayKey, type KeyNotation } from './key-notation';

/** A table font: what the active skin renders the table in (theme.css
 * `--rb-font` / `--rb-fs-browser`, read live by `renderedTableFont`). */
export interface TableFont {
	family: string;
	sizePx: number;
}

/** Width in CSS px of `text` drawn in the CSS font shorthand `font`. */
export type MeasureText = (text: string, font: string) => number;

export const KEY_STATUS_WORDS = ['missing', 'failed'] as const;

/** `.th-label` gap plus the 8px SortArrowIcon. */
const HEADER_ARROW_PX = 3 + 8;
/** Sub-pixel rounding plus the 1px compat ring the key and BPM cells can draw. */
const CELL_SLACK_PX = 2;

export function cssFont(font: TableFont, weight: 400 | 700 = 400): string {
	return `${weight} ${font.sizePx}px ${font.family}`;
}

/** A row as the table holds it: the key string plus what the BPM cell reads. */
export type MusicalFitRow = Parameters<typeof bpmCellText>[0] & { key: string | null };

export interface MusicalFitInput {
	rows: readonly MusicalFitRow[];
	notation: KeyNotation;
	font: TableFont;
	/** `--tt-td-pad-x` for the current density. */
	padX: number;
	/** What the Key header prints (the master key in `notation`, or "K"). */
	keyHeader: string;
	/** What the BPM header prints (the master BPM, or "B"). */
	bpmHeader: string;
	measure: MeasureText;
}

function _cellWidth(texts: Iterable<string>, font: string, padX: number, measure: MeasureText): number {
	let widest = 0;
	for (const text of texts) {
		if (text === '') continue;
		const width = measure(text, font);
		if (!Number.isFinite(width) || width < 0) {
			throw new RangeError(`measureText returned ${width} for ${JSON.stringify(text)}`);
		}
		widest = Math.max(widest, width);
	}
	return widest + 2 * padX + CELL_SLACK_PX;
}

function _headerWidth(text: string, input: MusicalFitInput): number {
	const width = input.measure(text, cssFont(input.font, 700));
	return width + HEADER_ARROW_PX + 2 * input.padX + CELL_SLACK_PX;
}

function* _keyTexts(rows: readonly MusicalFitRow[], notation: KeyNotation): Generator<string> {
	yield* KEY_STATUS_WORDS;
	for (const row of rows) {
		const shown = displayKey(row.key, notation);
		if (shown !== null) yield shown.text;
	}
}

function* _bpmTexts(rows: readonly MusicalFitRow[]): Generator<string> {
	yield* KEY_STATUS_WORDS;
	for (const row of rows) yield bpmCellText(row).text;
}

/** Key and BPM widths that show every string the columns print, unclipped. */
export function fitMusicalColumns(input: MusicalFitInput): { key: number; bpm: number } {
	if (!Number.isFinite(input.padX) || input.padX < 0) {
		throw new RangeError(`padX must be a non-negative number, got ${input.padX}`);
	}
	const font = cssFont(input.font);
	const key = Math.max(
		_cellWidth(_keyTexts(input.rows, input.notation), font, input.padX, input.measure),
		_headerWidth(input.keyHeader, input)
	);
	const bpm = Math.max(
		_cellWidth(_bpmTexts(input.rows), font, input.padX, input.measure),
		_headerWidth(input.bpmHeader, input)
	);
	return { key: Math.ceil(key), bpm: Math.ceil(bpm) };
}

/** The font an element really renders in, read from computed style, so the
 * fit follows whichever skin (and fallback font) is live. */
export function renderedTableFont(el: Element): TableFont {
	const style = getComputedStyle(el);
	const sizePx = Number.parseFloat(style.fontSize);
	if (!Number.isFinite(sizePx) || sizePx <= 0) throw new Error(`table font size unreadable: ${style.fontSize}`);
	return { family: style.fontFamily, sizePx };
}

let _canvasContext: CanvasRenderingContext2D | null = null;

/** The browser measurer: canvas `measureText`, in the exact font string the
 * table renders with. Throws outside a browser rather than guessing. */
export function canvasMeasureText(text: string, font: string): number {
	if (_canvasContext === null) {
		if (typeof document === 'undefined') {
			throw new Error('canvasMeasureText needs a DOM; pass a measurer in tests');
		}
		_canvasContext = document.createElement('canvas').getContext('2d');
		if (_canvasContext === null) throw new Error('2d canvas context unavailable');
	}
	_canvasContext.font = font;
	return _canvasContext.measureText(text).width;
}
