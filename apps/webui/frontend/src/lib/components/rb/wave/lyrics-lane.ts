/** Track-time projection shared by the lyrics overlay and its regression tests. */

export type TimedLyricLine = { start_ms: number };

/** One rendered lyric line - the shape `LyricsLane.svelte` actually needs,
 * kept here (not re-imported from `$lib/rb/api-rb`'s `TrackLyrics`) so the
 * component depends only on this module's display-projection types. */
export type LyricLine = { start_ms: number; text: string };

/** CSS percent across a waveform window centered on the fixed playhead. */
export function lyricLanePositionPercent({
	lineStartMs,
	positionMs,
	pitch,
	windowSeconds
}: {
	lineStartMs: number;
	positionMs: number;
	pitch: number;
	windowSeconds: number;
}): number {
	if (!Number.isFinite(lineStartMs) || !Number.isFinite(positionMs)) {
		throw new Error('lyric lane timestamps must be finite');
	}
	if (!Number.isFinite(pitch) || pitch <= 0 || !Number.isFinite(windowSeconds) || windowSeconds <= 0) {
		throw new Error('lyric lane requires positive pitch and windowSeconds');
	}
	return 50 + ((lineStartMs - positionMs) / (windowSeconds * pitch * 1000)) * 100;
}

/**
 * Widest a lane entry may be, as CSS percent of the waveform window: the
 * distance to the next entry's start. An entry is drawn from its own start
 * rightwards, so capping it here means two entries can never overlap.
 * null = last entry, nothing to run into.
 */
export function lyricLaneWidthPercent({
	lineStartMs,
	nextStartMs,
	pitch,
	windowSeconds
}: {
	lineStartMs: number;
	nextStartMs: number | null;
	pitch: number;
	windowSeconds: number;
}): number | null {
	if (nextStartMs === null) return null;
	if (!Number.isFinite(lineStartMs) || !Number.isFinite(nextStartMs)) {
		throw new Error('lyric lane timestamps must be finite');
	}
	if (nextStartMs <= lineStartMs) {
		throw new Error(`the next lyric lane entry must start later: ${nextStartMs} <= ${lineStartMs}`);
	}
	if (!Number.isFinite(pitch) || pitch <= 0 || !Number.isFinite(windowSeconds) || windowSeconds <= 0) {
		throw new Error('lyric lane requires positive pitch and windowSeconds');
	}
	return ((nextStartMs - lineStartMs) / (windowSeconds * pitch * 1000)) * 100;
}

/** The active line is the latest cache timestamp at or before the playhead. */
export function activeLyricLineIndex(lines: readonly TimedLyricLine[], positionMs: number): number {
	let activeIndex = -1;
	for (let index = 0; index < lines.length; index += 1) {
		if (lines[index].start_ms > positionMs) return activeIndex;
		activeIndex = index;
	}
	return activeIndex;
}

/** Consecutive lines sharing one timestamp, drawn as a single lane entry. */
export type LyricLaneGroup = { start_ms: number; text: string; firstIndex: number; lastIndex: number };

/** Merge equal-timestamp lines (LYRICS-09 duets) so the lane never stacks two
 * spans at one `left` or keys two entries by the same start_ms. */
export function lyricLaneGroups(lines: readonly LyricLine[]): LyricLaneGroup[] {
	const groups: LyricLaneGroup[] = [];
	lines.forEach((line, index) => {
		const last = groups.at(-1);
		if (last !== undefined && last.start_ms === line.start_ms) {
			last.text = `${last.text} / ${line.text}`;
			last.lastIndex = index;
		} else {
			groups.push({ start_ms: line.start_ms, text: line.text, firstIndex: index, lastIndex: index });
		}
	});
	return groups;
}

/** Lines whose timestamps fall inside a loop in/out window (inclusive). */
export function lyricLinesSpanningRange(
	lines: readonly LyricLine[],
	inMs: number,
	outMs: number
): LyricLine[] {
	if (outMs < inMs) return [];
	let startIndex = activeLyricLineIndex(lines, inMs);
	if (startIndex < 0) {
		startIndex = lines.findIndex((line) => line.start_ms >= inMs);
		if (startIndex < 0) return [];
	}
	const spanning: LyricLine[] = [];
	for (let index = startIndex; index < lines.length; index += 1) {
		if (lines[index].start_ms > outMs) break;
		spanning.push(lines[index]);
	}
	return spanning;
}

//--------------------------------------------------------------- two-row layout

/** Lyric rows over a main wave row (design record: re-skinning
 * design-widgets, option G, Mon 5 Oct 2026). Entry i sits on row i % 2, so
 * neighbors never share a row; row centers as a percent of the row height. */
export const LYRIC_ROW_CENTER_PCT = [32, 68] as const;
/** Clear pixels kept between two entries sharing a row. */
export const LYRIC_ENTRY_GAP_PX = 6;

/** One lane entry's row and the widest it may draw before it would reach the
 * next entry on the SAME row (entry i + 2), as a percent of the lane width.
 * Infinity when no later entry shares the row. */
export type LyricLaneSlot = { row: 0 | 1; maxWidthPct: number };

export function lyricLaneSlots(
	entries: readonly TimedLyricLine[],
	pitch: number,
	windowSeconds: number
): LyricLaneSlot[] {
	if (!Number.isFinite(pitch) || pitch <= 0 || !Number.isFinite(windowSeconds) || windowSeconds <= 0) {
		throw new Error('lyricLaneSlots requires positive pitch and windowSeconds');
	}
	const pctPerMs = 100 / (windowSeconds * pitch * 1000);
	return entries.map((entry, index) => {
		const sameRowNext = entries[index + 2];
		return {
			row: (index % 2) as 0 | 1,
			maxWidthPct:
				sameRowNext === undefined ? Infinity : (sameRowNext.start_ms - entry.start_ms) * pctPerMs
		};
	});
}
