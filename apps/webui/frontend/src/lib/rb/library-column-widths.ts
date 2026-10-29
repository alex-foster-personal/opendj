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
	key: 64,
	bpm: 46,
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

/** LIBUX-54: column widths with every hidden column at 0, for the sums that
 * place the table width and the MASTER fold badge. `hidden` is ui-prefs
 * `library_hidden_columns` (see library-columns.ts for which ids may appear). */
export function shownColumnWidths<W extends Record<ColId, number>>(widths: W, hidden: readonly ColId[]): W {
	const out = { ...widths };
	for (const col of hidden) out[col] = 0 as W[typeof col];
	return out;
}

/** LIBUX-54: class names for the table root, `hide-key hide-bpm`. Each one
 * hides that column's header (`th.h-<col>`) and cells (`td.c-<col>`). */
export function hiddenColumnClasses(hidden: readonly ColId[]): string {
	return hidden.map((col) => `hide-${col}`).join(' ');
}

// Key and BPM auto-fit lives in musical-column-fit.ts: it measures the text
// each column prints in the active skin's font. These are only the widths a
// column starts at before its first fit (wide enough for `8A • Am` in Gothic).
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
