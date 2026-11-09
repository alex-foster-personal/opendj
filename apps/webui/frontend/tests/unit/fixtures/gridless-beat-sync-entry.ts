/**
 * Shared-graph entry for the issue #2607 real-load tests.
 *
 * loadTypeScriptModule bundles each entry independently; the engine, the
 * IPC dispatcher and the toast store must share ONE module graph so a deck
 * the engine loaded is the deck the dispatcher reads and the toast the engine
 * pushed is the toast the test inspects.
 */
export { deckLoadErrors, deckStates, engine, getDeckState } from '$lib/rb/audio-engine.svelte';
export {
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	runPerformanceCommandFromUi,
	queryPerformanceState
} from '$lib/rb/performance-ipc.svelte';
export { toasts } from '$lib/stores.svelte';
export {
	effectiveBeatSync,
	hasPhaseCapableBeatGrid,
	hasTrustedBeatGrid
} from '$lib/player/grid-features';
