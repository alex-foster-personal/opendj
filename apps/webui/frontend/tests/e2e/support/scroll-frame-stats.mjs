/** Frame-interval stats for the library scroll frame bench (PERF-CAPTURE-04, issue #10230). */

export const MIN_FRAMES = 100;
/** A frame delta strictly above this is long; matches the Long Tasks API 50 ms bar. */
export const LONG_FRAME_MS = 50;
export const CAP_HEADROOM = 1.2;

const round1 = (x) => Math.round(x * 10) / 10;

export function nearestRankPercentile(values, p) {
	if (values.length === 0) throw new Error('no frames sampled');
	const sorted = [...values].sort((a, b) => a - b);
	return sorted[Math.max(0, Math.ceil((p / 100) * sorted.length) - 1)];
}

export function summarizeFrameIntervals(deltasMs) {
	if (deltasMs.length === 0) throw new Error('no frames sampled');
	deltasMs.forEach((d, i) => {
		if (!Number.isFinite(d) || d < 0)
			throw new Error(`frame delta ${i} is not a valid ms value: ${d}`);
	});
	return {
		frames: deltasMs.length,
		interval_p50_ms: round1(nearestRankPercentile(deltasMs, 50)),
		interval_p95_ms: round1(nearestRankPercentile(deltasMs, 95)),
		interval_max_ms: round1(Math.max(...deltasMs)),
		long_frames: deltasMs.filter((d) => d > LONG_FRAME_MS).length
	};
}

export function refusalReason({ frames, rowsMounted, scrolledPx }) {
	if (frames < MIN_FRAMES) return `only ${frames} frames sampled, need at least ${MIN_FRAMES}`;
	if (rowsMounted === 0) return 'no track row mounted in the track table tbody';
	if (scrolledPx <= 0) return 'the wheel did not scroll the track list (scrollTop never moved)';
	return null;
}

export function capFromMeasured(metric, measured) {
	if (metric === 'interval_p95_ms') return round1(measured * CAP_HEADROOM);
	if (metric === 'blocking_wheel_listeners') return Math.floor(measured * CAP_HEADROOM);
	throw new Error(`no cap rule for metric ${metric}`);
}

export function checkCaps(measured, caps) {
	return Object.entries(caps).map(([metric, cap]) => {
		const value = measured[metric];
		if (typeof value !== 'number') throw new Error(`measured lacks capped metric ${metric}`);
		const ok = value <= cap;
		const diff = round1(Math.abs(value - cap)).toFixed(1);
		const arithmetic = ok
			? `${metric} ${value} <= cap ${cap} (${diff} under)`
			: `${metric} ${value} > cap ${cap} (${value} - ${cap} = +${diff} over)`;
		return { metric, measured: value, cap, ok, arithmetic };
	});
}
