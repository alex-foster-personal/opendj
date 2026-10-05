/** Library column sizing. Utility cells auto-fit on load and viewport resize;
 * other widths remain user-controlled. Analysis grids are 14px plus 2px padding
 * per side. Row numbers use 9px tabular digits, with space for the reorder grip.
 */
export const COL_DEFAULTS = {
	funnel: 18,
	err: 20,
	// 12px cloud glyph + 2px gap + 9px minor-issue square + 2px padding per side.
	cloud: 28,
	order: 24,
	preview: 177,
	art: 54,
	title: 220,
	artist: 140,
	key: 36,
	bpm: 42,
	plays: 36,
	rating: 80,
	comments: 110,
	time: 48,
	quality: 34,
	energy: 22,
	genre: 90,
	stems: 148,
	lyrics: 64,
	// 12px robot icon with 8px breathing room on each side.
	autoplay: 28
} as const;

export type ColId = keyof typeof COL_DEFAULTS;

type MusicalRow = {
	key: string | null;
	bpm: number | null;
};

// WebKit measures rendered 10B at 29.23px and 124 at 33px once the table cell
// padding is included, so compact columns must not go below their rounded values.
const KEY_MIN_WIDTH = 30;
const BPM_MIN_WIDTH = 33;
const MUSICAL_CHAR_WIDTH = 7;
const MUSICAL_CELL_PADDING = 7;

function _compactTextWidth(text: string, minimum: number, maximum: number): number {
	return Math.min(maximum, Math.max(minimum, text.length * MUSICAL_CHAR_WIDTH + MUSICAL_CELL_PADDING));
}

/** Compact K/B widths from the text the table actually displays. */
export function compactMusicalWidths(rows: readonly MusicalRow[]): Pick<Record<ColId, number>, 'key' | 'bpm'> {
	let key = KEY_MIN_WIDTH;
	let bpm = BPM_MIN_WIDTH;
	for (const row of rows) {
		key = Math.max(key, _compactTextWidth(row.key ?? '', KEY_MIN_WIDTH, COL_DEFAULTS.key));
		bpm = Math.max(
			bpm,
			_compactTextWidth(row.bpm === null ? '' : String(Math.round(row.bpm)), BPM_MIN_WIDTH, COL_DEFAULTS.bpm)
		);
	}
	return { key, bpm };
}

/** Keep a user's K/B drag authoritative while other rows reflow. */
export function autoMusicalWidths(
	current: Pick<Record<ColId, number>, 'key' | 'bpm'>,
	automatic: Pick<Record<ColId, number>, 'key' | 'bpm'>,
	manuallyResized: ReadonlySet<ColId>
): Pick<Record<ColId, number>, 'key' | 'bpm'> {
	return {
		key: manuallyResized.has('key') ? current.key : automatic.key,
		bpm: manuallyResized.has('bpm') ? current.bpm : automatic.bpm
	};
}

export function compactUtilityWidths(
	maxRowOrder: number
): Pick<Record<ColId, number>, 'funnel' | 'err' | 'cloud' | 'order'> {
	if (!Number.isSafeInteger(maxRowOrder) || maxRowOrder < 0) {
		throw new RangeError('maxRowOrder must be a non-negative safe integer');
	}
	return {
		funnel: COL_DEFAULTS.funnel,
		err: COL_DEFAULTS.err,
		cloud: COL_DEFAULTS.cloud,
		order: Math.max(COL_DEFAULTS.order, String(maxRowOrder).length * 5 + 12)
	};
}
