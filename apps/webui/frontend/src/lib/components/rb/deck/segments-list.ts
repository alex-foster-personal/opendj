/**
 * STRUCT-9657: the row model behind the Segments tab of the HOT CUE
 * dropdown (PadModeMenu.svelte). Pure: no Svelte, no DOM, no deck store,
 * so node unit-tests it directly (tests/unit/segments-list.test.mjs).
 *
 * Each SectionSpan becomes one row: full label, start bar, length in bars
 * and start time `m:ss`. Bars come from the beat grid's downbeats (n == 1);
 * with no downbeats the bar cells are null and render as a dash, never a
 * guessed number. The playing row is the last section starting at or
 * before the playhead, while the playhead is still inside it.
 */
import { formatClock } from '$lib/lyrics/stage-frame';
import type { AnlzBeat } from '$lib/rb/anlz-types';
import { HOT_CUE_SLOTS } from '$lib/rb/auto-cue-proposals';
import type { HotCue, HotCueSlot } from '$lib/rb/hot-cue-types';
import type { SectionSpan } from '$lib/components/rb/wave/sections';

/** Shown instead of rows when the track has no sections (not an error). */
export const NO_SECTIONS_MESSAGE = 'No sections for this track';
/** Shown instead of saving when every hot cue slot already holds a cue. */
export const HOT_CUES_FULL_MESSAGE = 'All 8 hot cues are full';

/** A section boundary within this many seconds of a downbeat counts as on it. */
const BAR_EPSILON_S = 0.05;

/** One Segments tab row. */
export interface SegmentRow {
	label: string;
	/** Section start in ms, the `seek` / `hot_cue_save` target. */
	start_ms: number;
	/** 1-based bar the section starts in; null when the track has no downbeats. */
	start_bar: number | null;
	/** Whole bars the section spans; null when the track has no downbeats. */
	length_bars: number | null;
	/** Start time as `m:ss`. */
	time: string;
}

/** The rows plus which one holds the playhead (-1 when none does). */
export interface SegmentsModel {
	rows: SegmentRow[];
	playing_index: number;
}

/** How many downbeats sit at or before `t`. */
function _downbeatsAtOrBefore(downbeats: readonly number[], t: number): number {
	let lo = 0;
	let hi = downbeats.length;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (downbeats[mid] <= t) lo = mid + 1;
		else hi = mid;
	}
	return lo;
}

/** Index of the section holding `positionS`, or -1 before the first or after the last. */
export function playingSegmentIndex(spans: readonly SectionSpan[], positionS: number): number {
	if (!Number.isFinite(positionS)) return -1;
	for (let i = spans.length - 1; i >= 0; i--) {
		if (spans[i].start_s <= positionS) return positionS < spans[i].end_s ? i : -1;
	}
	return -1;
}

/** Build the Segments rows for sorted section spans, the deck's beat grid and the playhead. */
export function buildSegmentRows(
	spans: readonly SectionSpan[],
	beats: readonly AnlzBeat[] | null | undefined,
	positionMs: number
): SegmentsModel {
	const downbeats = (beats ?? []).filter((b) => b.n === 1).map((b) => b.t);
	const hasBars = downbeats.length > 0;
	const rows = spans.map((span): SegmentRow => {
		// Bars whose downbeat falls inside [start, end): a boundary on a downbeat
		// belongs to the section it opens, not the one it closes.
		const before = _downbeatsAtOrBefore(downbeats, span.start_s - BAR_EPSILON_S);
		const through = _downbeatsAtOrBefore(downbeats, span.end_s - BAR_EPSILON_S);
		return {
			label: span.label,
			start_ms: Math.round(span.start_s * 1000),
			start_bar: hasBars
				? Math.max(1, _downbeatsAtOrBefore(downbeats, span.start_s + BAR_EPSILON_S))
				: null,
			length_bars: hasBars ? Math.max(1, through - before) : null,
			time: formatClock(span.start_s)
		};
	});
	return { rows, playing_index: playingSegmentIndex(spans, positionMs / 1000) };
}

/** First hot cue slot A..H with no cue in it, or null when all 8 are taken. */
export function firstEmptyHotCueSlot(hotCues: readonly Pick<HotCue, 'slot'>[]): HotCueSlot | null {
	const used = new Set(hotCues.map((c) => c.slot));
	return HOT_CUE_SLOTS.find((slot) => !used.has(slot)) ?? null;
}
