/** p50/p95 helpers for library-playlist-switch latency bench (PERF-UI-05). */

export function percentile(sortedAsc, p) {
	if (sortedAsc.length === 0) {
		throw new Error('percentile requires at least one sample');
	}
	const rank = (p / 100) * (sortedAsc.length - 1);
	const lower = Math.floor(rank);
	const upper = Math.ceil(rank);
	if (lower === upper) return sortedAsc[lower];
	const weight = rank - lower;
	return sortedAsc[lower] * (1 - weight) + sortedAsc[upper] * weight;
}

export function summarizeSamples(samples) {
	const sorted = [...samples].sort((a, b) => a - b);
	return {
		p50: Math.round(percentile(sorted, 50)),
		p95: Math.round(percentile(sorted, 95))
	};
}

// playlist_tree_ready_ms is open-to-tree, counted from navigation start. Until
// #3985 the app recorded `now() - timeOrigin` clamped to 0, so this metric read
// 0 ms on every run and the old 150 ms cap never measured anything. 2000 ms is
// the issue's own acceptance ("playlists load in <2 seconds").
export const THRESHOLDS_P50_MS = {
	playlist_tree_ready_ms: 2000,
	playlist_switch_first_rows_ms: 100,
	all_tracks_first_rows_ms: 100
};
