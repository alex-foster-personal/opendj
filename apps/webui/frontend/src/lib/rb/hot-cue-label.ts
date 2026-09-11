import {
	activeLyricLineIndex,
	lyricLinesSpanningRange
} from '$lib/components/rb/wave/lyrics-lane';
import type { AnlzBeat } from './anlz-types';
import type { HotCue } from './hot-cue-types';

function formatCueTime(ms: number): string {
	const seconds = Math.floor(ms / 1000);
	return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}.${String(Math.round(ms % 1000)).padStart(3, '0')}`;
}

/** Fractional interval index on the actual PQTZ grid, never tag-BPM extrapolation. */
function beatCoordinate(beats: readonly Pick<AnlzBeat, 't'>[], time: number): number | null {
	if (beats.length < 2 || time < beats[0].t || time > beats[beats.length - 1].t) return null;
	let low = 0;
	let high = beats.length - 1;
	while (low < high) {
		const middle = (low + high) >>> 1;
		if (beats[middle].t < time) low = middle + 1;
		else high = middle;
	}
	if (beats[low].t === time) return low;
	const previous = low - 1;
	const interval = beats[low].t - beats[previous].t;
	return interval > 0 ? previous + (time - beats[previous].t) / interval : null;
}

function insertLyricChevron(text: string, startMs: number, endMs: number | null, atMs: number): string {
	const words = text.trim().split(/\s+/).filter(Boolean);
	if (words.length === 0) return text;
	let fraction = 0;
	if (endMs !== null && endMs > startMs) {
		fraction = Math.max(0, Math.min(1, (atMs - startMs) / (endMs - startMs)));
	}
	const index = Math.min(words.length - 1, Math.floor(fraction * words.length));
	words[index] = `▸${words[index]}`;
	return words.join(' ');
}

type LyricInput = { start_ms: number; text: string };

function pointCueLyricRows(lyrics: readonly LyricInput[], atMs: number): string[] {
	const activeIndex = activeLyricLineIndex(lyrics, atMs);
	if (activeIndex < 0) return [];
	const line = lyrics[activeIndex];
	const endMs = activeIndex + 1 < lyrics.length ? lyrics[activeIndex + 1].start_ms : null;
	return [insertLyricChevron(line.text, line.start_ms, endMs, atMs)];
}

function loopCueLyricRows(lyrics: readonly LyricInput[], inMs: number, outMs: number): string[] {
	return lyricLinesSpanningRange(lyrics, inMs, outMs).map((line) => line.text.trim());
}

export function hotCueTitle(
	cue: HotCue,
	beats: readonly Pick<AnlzBeat, 't'>[],
	lyrics: readonly LyricInput[] = []
): string {
	const label = cue.comment ?? `hot cue ${cue.slot}`;
	if (!cue.is_loop || cue.out_ms === null) {
		const beat = beatCoordinate(beats, cue.in_ms / 1000);
		let context = `${label} - ${formatCueTime(cue.in_ms)}`;
		if (beat !== null) context += `; beat ${Number(beat.toFixed(2))} (PQTZ)`;
		const lyricRows = lyrics.length > 0 ? pointCueLyricRows(lyrics, cue.in_ms) : [];
		return lyricRows.length > 0 ? `${context}\n${lyricRows.join('\n')}` : context;
	}
	const endpoints = `${label} - loop ${formatCueTime(cue.in_ms)} to ${formatCueTime(cue.out_ms)}`;
	const start = beatCoordinate(beats, cue.in_ms / 1000);
	const end = beatCoordinate(beats, cue.out_ms / 1000);
	const context =
		start === null || end === null || end <= start
			? `${endpoints}; beat count unavailable`
			: `${endpoints}; ${Number((end - start).toFixed(2))} beats (${Number(((end - start) / 4).toFixed(2))} bars, PQTZ)`;
	const lyricRows = lyrics.length > 0 ? loopCueLyricRows(lyrics, cue.in_ms, cue.out_ms) : [];
	return lyricRows.length > 0 ? `${context}\n${lyricRows.join('\n')}` : context;
}
