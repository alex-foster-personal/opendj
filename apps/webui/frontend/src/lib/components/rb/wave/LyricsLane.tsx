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
// WAVE_WINDOW_S is restated, not imported: render.ts reaches api-rb.ts, whose
// type imports pull job-progress.svelte.ts into `tsc -p tsconfig.react.json`.
// It must equal render.ts WAVE_WINDOW_S; the contract pins the window (one
// percent of the lane = 240 ms at pitch 1), so a drift fails both stories.
const WAVE_WINDOW_S = 24;

export type LyricsLaneProps = {
	lyrics: { lines: LyricLine[] } | null;
	loadError: Error | null;
	positionMs: number;
	pitch: number;
};

export default function LyricsLane({ lyrics, loadError, positionMs, pitch }: LyricsLaneProps) {
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
	const slots = lyricLaneSlots(groups, pitch, WAVE_WINDOW_S);

	return (
		<div className="lyrics-lane" aria-label="Synced lyrics">
			{groups.map((group, groupIndex) => {
				const slot = slots[groupIndex];
				const active = activeIndex >= group.firstIndex && activeIndex <= group.lastIndex;
				const left = lyricLanePositionPercent({
					lineStartMs: group.start_ms,
					positionMs,
					pitch,
					windowSeconds: WAVE_WINDOW_S
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
