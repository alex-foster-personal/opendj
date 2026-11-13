/**
 * Pure filter + yellow-highlight split for the LIBUX-04 hotkeys overlay.
 *
 * Substring match, case-insensitive, not fuzzy. Empty query returns every
 * entry with no highlight marks. A query that matches nothing returns [].
 */

export type HotkeysOverlayView = 'grid' | 'list';

export interface HotkeyEntry {
	id: string;
	/** Human chord label shown in the overlay, e.g. "Cmd+," or "/". */
	chord: string;
	description: string;
	group: string;
}

export interface HighlightPiece {
	text: string;
	matched: boolean;
}

export interface FilteredHotkey {
	entry: HotkeyEntry;
	chordPieces: HighlightPiece[];
	descriptionPieces: HighlightPiece[];
	groupPieces: HighlightPiece[];
}

function _haystack(entry: HotkeyEntry): string {
	return `${entry.chord} ${entry.description} ${entry.group}`.toLowerCase();
}

/**
 * Split `text` into unmatched / matched runs for the first and later
 * case-insensitive occurrences of `query`. Empty or whitespace-only query
 * yields a single unmatched piece (the original text).
 */
export function highlightPieces(text: string, query: string): HighlightPiece[] {
	const needle = query.trim();
	if (needle.length === 0) return [{ text, matched: false }];
	const lowerText = text.toLowerCase();
	const lowerNeedle = needle.toLowerCase();
	let start = 0;
	let idx = lowerText.indexOf(lowerNeedle, start);
	if (idx < 0) return [{ text, matched: false }];
	const pieces: HighlightPiece[] = [];
	while (idx >= 0) {
		if (idx > start) {
			pieces.push({ text: text.slice(start, idx), matched: false });
		}
		pieces.push({ text: text.slice(idx, idx + needle.length), matched: true });
		start = idx + needle.length;
		idx = lowerText.indexOf(lowerNeedle, start);
	}
	if (start < text.length) {
		pieces.push({ text: text.slice(start), matched: false });
	}
	return pieces;
}

/** Filter `entries` by a case-insensitive substring of chord, description, or group. */
export function filterHotkeys(entries: readonly HotkeyEntry[], query: string): FilteredHotkey[] {
	const needle = query.trim();
	if (needle.length === 0) {
		return entries.map((entry) => ({
			entry,
			chordPieces: [{ text: entry.chord, matched: false }],
			descriptionPieces: [{ text: entry.description, matched: false }],
			groupPieces: [{ text: entry.group, matched: false }]
		}));
	}
	const lower = needle.toLowerCase();
	const out: FilteredHotkey[] = [];
	for (const entry of entries) {
		if (!_haystack(entry).includes(lower)) continue;
		out.push({
			entry,
			chordPieces: highlightPieces(entry.chord, needle),
			descriptionPieces: highlightPieces(entry.description, needle),
			groupPieces: highlightPieces(entry.group, needle)
		});
	}
	return out;
}

//----- #12288: one polite live-region summary of the filter result

/** Shortcuts the overlay shows for `rows`; a row id listed twice counts once. */
export function visibleShortcutCount(rows: readonly FilteredHotkey[]): number {
	return new Set(rows.map((row) => row.entry.id)).size;
}

/** The live-region sentence for a result count: "No shortcuts found", "1 shortcut found", "5 shortcuts found". */
export function shortcutCountAnnouncement(count: number): string {
	if (!Number.isInteger(count) || count < 0) {
		throw new Error(`shortcut count must be a non-negative integer, got ${count}`);
	}
	if (count === 0) return 'No shortcuts found';
	if (count === 1) return '1 shortcut found';
	return `${count} shortcuts found`;
}
