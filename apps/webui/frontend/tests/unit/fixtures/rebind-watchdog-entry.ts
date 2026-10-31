// One bundle holding both modules, so the re-bind and the context watchdog
// share the module state that marks a suspend as expected (issue #5887).
export * from '$lib/rb/audio-output-rebind';
export {
	beginExpectedSuspend,
	installAudioContextWatchdog,
	noteRecoveryOpportunity
} from '$lib/rb/audio-context-watchdog';
