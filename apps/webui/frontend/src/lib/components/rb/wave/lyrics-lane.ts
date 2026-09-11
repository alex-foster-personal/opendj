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

/** The active line is the latest cache timestamp at or before the playhead. */
export function activeLyricLineIndex(lines: readonly TimedLyricLine[], positionMs: number): number {
	let activeIndex = -1;
	for (let index = 0; index < lines.length; index += 1) {
		if (lines[index].start_ms > positionMs) return activeIndex;
		activeIndex = index;
	}
	return activeIndex;
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
