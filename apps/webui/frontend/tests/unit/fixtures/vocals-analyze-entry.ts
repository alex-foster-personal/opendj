/**
 * Entry for the vocal-analysis-trigger tests (PARITY-08 / issue #1038).
 *
 * Same reasoning as stems-jobs-entry.ts: one bundle, so the test exercises
 * the exact module the UI button imports rather than a hand-copied stand-in.
 */
export * from '$lib/rb/api-rb';
export * from '$lib/rb/vocals-analyze-refusal';
