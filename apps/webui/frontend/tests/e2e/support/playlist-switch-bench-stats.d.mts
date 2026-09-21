// Types for playlist-switch-bench-stats.mjs, which the PERF-UI-05 spec imports
// (tsconfig has allowJs off, so an .mjs helper needs its own declaration; same
// shape as ../kpi-capture-timeouts.d.mts).
export declare function percentile(sortedAsc: readonly number[], p: number): number;
export declare function summarizeSamples(samples: readonly number[]): { p50: number; p95: number };
export declare const THRESHOLDS_P50_MS: {
	playlist_tree_ready_ms: number;
	playlist_switch_first_rows_ms: number;
	all_tracks_first_rows_ms: number;
};
