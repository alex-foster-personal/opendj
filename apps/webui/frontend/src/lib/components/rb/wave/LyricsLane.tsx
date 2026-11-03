import {
	activeLyricLineIndex,
	lyricLaneGroups,
	lyricLanePositionPercent,
	lyricLaneSlots,
	LYRIC_ENTRY_GAP_PX,
	LYRIC_ROW_CENTER_PCT,
	type LyricLine
} from './lyrics-lane';

import './LyricsLane.react.css';

// React twin of LyricsLane.svelte: same DOM, classes, ARIA and behavior,
// held to the same contract (LyricsLane.contract.ts). positionMs arrives as a
// prop exactly as in the original; no per-frame state lives here.
//

export type LyricsLaneProps = {
	lyrics: { lines: LyricLine[] } | null;
	loadError: Error | null;
	positionMs: number;
	pitch: number;
	/** The row's wall-clock window and playhead place, as the Svelte twin takes them (WAVEZOOM-6802). */
	windowSeconds: number;
	playheadFrac: number;
};

export default function LyricsLane({
	lyrics,
	loadError,
	positionMs,
	pitch,
	windowSeconds,
	playheadFrac
}: LyricsLaneProps) {
	if (loadError !== null) {
		return (
			<span className="lyrics-error" title={loadError.message}>
				LYRICS ERROR
			</span>
		);
	}
	if (lyrics === null) return null;

	const activeIndex = activeLyricLineIndex(lyrics.lines, positionMs);
	const groups = lyricLaneGroups(lyrics.lines);
	const slots = lyricLaneSlots(groups, pitch, windowSeconds);

	return (
		<div className="lyrics-lane" aria-label="Synced lyrics">
			{groups.map((group, groupIndex) => {
				const slot = slots[groupIndex];
				const active = activeIndex >= group.firstIndex && activeIndex <= group.lastIndex;
				const left = lyricLanePositionPercent({
					lineStartMs: group.start_ms,
					positionMs,
					pitch,
					windowSeconds,
					playheadFrac
				});
				// Two-row slots clamp each entry to the next entry on ITS row
				// (i + 2), which subsumes the adjacent-entry clamp.
				const maxWidth = Number.isFinite(slot.maxWidthPct) ? slot.maxWidthPct : null;
				// Drawn from its start rightwards, so a line stays on screen
				// until its END leaves the left edge, not its start.
				if (!(left <= 110 && (maxWidth === null ? left >= -100 : left + maxWidth >= 0))) return null;
				return (
					<span
						key={group.start_ms}
						className={active ? 'lyric-line active' : 'lyric-line'}
						style={{
							left: `${left}%`,
							top: `${LYRIC_ROW_CENTER_PCT[slot.row]}%`,
							maxWidth: maxWidth === null ? undefined : `calc(${maxWidth}% - ${LYRIC_ENTRY_GAP_PX}px)`
						}}
						data-row={slot.row}
						aria-current={active ? 'true' : undefined}
					>
						{group.text}
					</span>
				);
			})}
		</div>
	);
}
