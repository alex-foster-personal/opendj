/**
 * The typed dispatcher WITHOUT the separation store in the graph (STEM-77):
 * the test bundles it with the store's module failing to load, the way a
 * stale deployment's missing chunk fails in the browser, or with it loading
 * on demand, as the first `stem_separate` command loads it.
 */
export { deckStates } from '$lib/rb/audio-engine.svelte';
export { installPerformanceBrowserIpc } from '$lib/rb/performance-ipc.svelte';
export { loadedStemSeparation, separateLoadedDeck, stemSeparation, stemSeparationLoadError } from '$lib/rb/stem-separation-port';
export { loadingStemDeckState, unavailableStemDeckState } from '$lib/rb/stem-graph';
