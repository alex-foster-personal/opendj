/**
 * Shared-graph entry for AutoPlay Next transition + fader regression tests.
 */
export {
	deckStates,
	engine,
	getDeckState,
	peekDeckFaderGain
} from '$lib/rb/audio-engine.svelte';
export {
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc
} from '$lib/rb/performance-ipc.svelte';
export {
	armAutoPlayNext,
	autoPlayNextState,
	cancelAutoPlayNext
} from '$lib/rb/auto-play-next.svelte';
