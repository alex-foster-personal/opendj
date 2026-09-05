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

export function hotCueTitle(cue: HotCue, beats: readonly Pick<AnlzBeat, 't'>[]): string {
	const label = cue.comment ?? `hot cue ${cue.slot}`;
	if (!cue.is_loop || cue.out_ms === null) return `${label} - ${formatCueTime(cue.in_ms)}`;
	const endpoints = `${label} - loop ${formatCueTime(cue.in_ms)} to ${formatCueTime(cue.out_ms)}`;
	const start = beatCoordinate(beats, cue.in_ms / 1000);
	const end = beatCoordinate(beats, cue.out_ms / 1000);
	if (start === null || end === null || end <= start) return `${endpoints}; beat count unavailable`;
	const count = Number((end - start).toFixed(2));
	return `${endpoints}; ${count} beats (${Number(((end - start) / 4).toFixed(2))} bars, PQTZ)`;
}
