/**
 * Shared-graph entry for AutoPlay Next orchestrator tests.
 */
export { deckStates } from '$lib/rb/audio-engine.svelte';
export {
	armAutoPlayNext,
	autoPlayNextState,
	cancelAutoPlayNext
} from '$lib/rb/auto-play-next.svelte';
export {
	getDispatchPerformanceCommandCalls,
	resetDispatchPerformanceCommandStub,
	setDispatchPerformanceCommandStub
} from './auto-play-next-stub-ipc';
