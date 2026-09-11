/**
 * Shared-graph entry for silence-dropout-act honest-stop tests.
 *
 * performance-ipc is stubbed via esbuild alias so executeSilenceDropoutPlan
 * and deckStates share one bundle and one deckStates object.
 */
export { deckStates } from '$lib/rb/audio-engine.svelte';
export { executeSilenceDropoutPlan, handleSilenceDropoutPlan } from '$lib/rb/silence-dropout-act';
export { planSilenceDropout } from '$lib/rb/silence-dropout';
export {
	getDispatchPerformanceCommandCalls,
	resetDispatchPerformanceCommandStub,
	setDispatchPerformanceCommandStub
} from './silence-dropout-stub-ipc';
