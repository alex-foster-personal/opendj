/**
 * Shared-graph entry for silence-dropout-act honest-stop tests.
 *
 * performance-ipc is stubbed via esbuild alias so executeSilenceDropoutPlan
 * and deckStates share one bundle and one deckStates object.
 */
export { deckStates } from '$lib/rb/audio-engine.svelte';
export {
	executeSilenceDropoutPlan,
	handleSilenceDropoutPlan,
	onSilenceDropoutStopped
} from '$lib/rb/silence-dropout-act';
export {
	isSilenceRecovering,
	noteAutoPlayFollowerPlayDispatched,
	resetAutoPlaySilenceRecovery
} from '$lib/rb/autoplay-silence-recover';
export { foldSilenceSample, SILENT_WHILE_PLAYING_MS } from '$lib/rb/silence-watchdog';
export { planSilenceDropout } from '$lib/rb/silence-dropout';
export {
	getDispatchPerformanceCommandCalls,
	resetDispatchPerformanceCommandStub,
	setDispatchPerformanceCommandStub
} from './silence-dropout-stub-ipc';
