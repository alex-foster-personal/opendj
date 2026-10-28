/**
 * Shared-graph entry for the STEM-77 "Separate this track" tests.
 *
 * One bundle, so the capability probe, the jobs store, the separation store
 * and the typed performance dispatcher are the same instances the app wires
 * together: a job row upserted here is the row `stemSeparate.job()` and the
 * `stem_separate` command read. No stubs; the only seam is globalThis.fetch.
 */
export * from '$lib/api/capabilities.svelte';
export * from '$lib/rb/jobs-store.svelte';
export { engineLibraryDir, launchedConfig, stemSeparate } from '$lib/rb/stem-separate.svelte';
export { STEMS_JOB_KIND, enqueueStemsForPending } from '$lib/rb/stems-jobs.svelte';
export { deckStates } from '$lib/rb/audio-engine.svelte';
export { installPerformanceBrowserIpc } from '$lib/rb/performance-ipc.svelte';
export { SeparateEndProbe, lookForBundle, separateRunId } from '$lib/rb/stem-separate-probe';
export { loadedStemSeparation, noteStemsLoadedOnceSettled, stemLoadRun, stemSeparation } from '$lib/rb/stem-separation-port';
export { retryDeckStems } from '$lib/rb/stem-retry';
export { readyStemDeckState, unavailableStemDeckState } from '$lib/rb/stem-graph';
export { separateActionView } from '$lib/rb/stem-separate-view';
export { stemStatusView } from '$lib/rb/stem-status';
