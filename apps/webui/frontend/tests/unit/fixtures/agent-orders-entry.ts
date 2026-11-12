/** Production agent-order executor plus the session it dispatches through,
 * exposed together so a behavioral test shares one bundled module instance
 * (see pairing-snapshot-entry.ts for the same need). */
export { executeAgentOrder, rampStartValue, runRamp } from '$lib/rb/agent-orders';
export { dialAnimations } from '$lib/rb/dial-animations.svelte';
export {
	abortPreparedPerformancePreset,
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	preparePerformancePresetTransaction,
	queryPerformanceState
} from '$lib/rb/performance-ipc.svelte';
export { mixerState } from '$lib/player/state.svelte';
export { playIoTestBeeps, stopIoTestBeeps } from '$lib/player/io-device-test-audio';
