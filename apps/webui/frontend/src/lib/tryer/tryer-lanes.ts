/** Pure drawing helpers for the try-er's two-deck lanes (TRYER-16); TryerLanes.svelte paints what these return. */

/** (seconds, value) and (start, end) pairs as the lanes route sends them. */
export type Point = readonly number[];
export type Span = readonly number[];

/** Knob lines drawn over each deck's waveform: name, color and dash, in drawing order. */
export const LANE_KNOBS: readonly {
	name: 'fader' | 'eq_low' | 'filter';
	label: string;
	color: string;
	dash: number[];
}[] = [
	{ name: 'fader', label: 'volume (fader)', color: '#ffffff', dash: [] },
	{ name: 'eq_low', label: 'bass (low EQ)', color: '#ffc04d', dash: [6, 3] },
	{ name: 'filter', label: 'filter (center = off)', color: '#4dd8ff', dash: [2, 3] }
];

/** Pixel y of a 0..1 knob value inside a row: 1 at the top, 0 at the bottom, 2 px inset. */
export function laneY(value: number, top: number, rowPx: number): number {
	return top + 2 + (1 - value) * (rowPx - 4);
}

/** Peak columns resampled to the canvas width: the max of the source columns each pixel covers. */
export function peakColumns(
	peaks: readonly number[],
	width: number
): { x: number; w: number; peak: number }[] {
	if (peaks.length === 0 || width <= 0) return [];
	const columns = Math.min(peaks.length, Math.floor(width));
	const out: { x: number; w: number; peak: number }[] = [];
	for (let c = 0; c < columns; c++) {
		const from = Math.floor((c * peaks.length) / columns);
		const to = Math.max(from + 1, Math.floor(((c + 1) * peaks.length) / columns));
		out.push({
			x: (c * width) / columns,
			w: width / columns,
			peak: Math.max(...peaks.slice(from, to))
		});
	}
	return out;
}

/** A breakpoint curve's value at ``t``: the value just after a step there (``after``) or just before it (``before``). */
export function valueAt(points: readonly Point[], t: number, side: 'after' | 'before'): number {
	const lerp = (a: Point, b: Point) => a[1] + ((b[1] - a[1]) * (t - a[0])) / (b[0] - a[0]);
	if (side === 'after') {
		let i = -1;
		for (let k = 0; k < points.length && points[k][0] <= t; k++) i = k;
		if (i === -1) return points[0][1];
		return i === points.length - 1 ? points[i][1] : lerp(points[i], points[i + 1]);
	}
	const j = points.findIndex(([time]) => time >= t);
	if (j === -1) return points[points.length - 1][1];
	return j === 0 ? points[0][1] : lerp(points[j - 1], points[j]);
}

/** The breakpoint polyline cut to each span a deck plays, with interpolated endpoints at the span edges. */
export function segmentsWithin(points: readonly Point[], spans: readonly Span[]): Point[][] {
	if (points.length === 0) return [];
	return spans.map(([start, end]) => [
		[start, valueAt(points, start, 'after')] as Point,
		...points.filter(([t]) => t > start && t < end),
		[end, valueAt(points, end, 'before')] as Point
	]);
}
