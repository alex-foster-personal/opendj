/**
 * Types for `scroll-frame-stats.mjs` (PERF-CAPTURE-04).
 *
 * The implementation stays plain `.mjs` so `tests/unit/scroll-frame-stats.test.mjs`
 * runs it under `node --test`. This project sets `allowJs: false`, so
 * `library-scroll-frames.spec.ts` importing it needs these declarations for
 * `svelte-check`. The unit test pins this surface against the real module.
 */

export const MIN_FRAMES: number;
export const LONG_FRAME_MS: number;
export const CAP_HEADROOM: number;

export type CappedMetric = 'interval_p95_ms' | 'blocking_wheel_listeners';

/** Nearest-rank percentile; throws `no frames sampled` on an empty series. */
export function nearestRankPercentile(values: readonly number[], p: number): number;

export function summarizeFrameIntervals(deltasMs: readonly number[]): {
	frames: number;
	interval_p50_ms: number;
	interval_p95_ms: number;
	interval_max_ms: number;
	long_frames: number;
};

export function refusalReason(input: {
	frames: number;
	rowsMounted: number;
	scrolledPx: number;
}): string | null;

export function capFromMeasured(metric: CappedMetric, measured: number): number;

export function checkCaps(
	measured: Record<string, number>,
	caps: Record<string, number>
): { metric: string; measured: number; cap: number; ok: boolean; arithmetic: string }[];
