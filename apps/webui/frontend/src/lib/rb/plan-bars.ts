/**
 * Bar math shared by the plan executor and the waveform overlay. It lives apart from agent-plan.ts so WaveRow's path
 * reaches it without pulling the executor into /performance's first load.
 *
 * Bars are counted on a deck's DOWNBEATS (`n === 1`), as apps/autodj/profile.py numbers them.
 */

export function downbeatsMs(beatgrid: readonly { n: number; time_ms: number }[]): number[] {
	return beatgrid.filter((beat) => beat.n === 1).map((beat) => beat.time_ms);
}

/** 1-based fractional bar at `positionMs` (apps/autodj/profile.py `bar_at`). */
export function barAt(downbeats: readonly number[], positionMs: number): number {
	if (downbeats.length < 2) throw new Error('no_grid');
	const last = downbeats.length - 1;
	if (positionMs < downbeats[0]) return 1 + (positionMs - downbeats[0]) / (downbeats[1] - downbeats[0]);
	if (positionMs >= downbeats[last]) return 1 + last + (positionMs - downbeats[last]) / (downbeats[last] - downbeats[last - 1]);
	let lo = 0;
	let hi = last;
	while (hi - lo > 1) {
		const mid = (lo + hi) >> 1;
		if (downbeats[mid] <= positionMs) lo = mid;
		else hi = mid;
	}
	return 1 + lo + (positionMs - downbeats[lo]) / (downbeats[lo + 1] - downbeats[lo]);
}

/** The playhead (ms) at 1-based fractional bar `bar`: the inverse of `barAt`. */
export function msAtBar(downbeats: readonly number[], bar: number): number {
	if (downbeats.length < 2) throw new Error('no_grid');
	const last = downbeats.length - 1;
	const index = Math.floor(bar - 1);
	if (index < 0) return downbeats[0] + (bar - 1) * (downbeats[1] - downbeats[0]);
	if (index >= last) return downbeats[last] + (bar - 1 - last) * (downbeats[last] - downbeats[last - 1]);
	return downbeats[index] + (bar - 1 - index) * (downbeats[index + 1] - downbeats[index]);
}
