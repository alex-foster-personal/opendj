/**
 * Types for `playlist-switch-bench-stats.mjs` (PERF-UI-05).
 *
 * The implementation stays plain `.mjs` because `tests/unit/library-playlist-switch-bench.test.mjs`
 * imports it under `node --test`, with no compiler in the way. This project sets
 * `allowJs: false`, so TypeScript cannot infer anything from that file, and
 * `library-playlist-switch-latency.spec.ts` importing it was an implicit `any`
 * that `svelte-check` rejects.
 *
 * A declaration file can drift from the module it describes without anything
 * noticing, so `tests/unit/library-playlist-switch-bench.test.mjs` asserts the
 * real module's exported surface against what is declared here.
 */

/** Linear-interpolated percentile over an ASCENDING-sorted sample array. */
export function percentile(sortedAsc: readonly number[], p: number): number;

/** Sorts a copy of `samples` and returns its rounded p50 and p95. */
export function summarizeSamples(samples: readonly number[]): { p50: number; p95: number };

/** Per-metric p50 ceilings, in milliseconds. */
export const THRESHOLDS_P50_MS: {
	readonly playlist_tree_ready_ms: number;
	readonly playlist_switch_first_rows_ms: number;
	readonly all_tracks_first_rows_ms: number;
};
