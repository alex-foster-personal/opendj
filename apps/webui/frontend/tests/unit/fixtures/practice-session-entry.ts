// One bundle for practice-session.test.mjs: the practice helper, IPC, engine and gig runtime must
// share module instances, or the session it starts is invisible to the dispatcher under test.
export { installPracticeSession } from '$lib/components/rb/practice/practice-session';
export { engine } from '$lib/rb/audio-engine.svelte';
export {
	activePerformanceCommandSession,
	claimPerformanceMount,
	currentPerformanceMount,
	dispatchPerformanceCommand,
	queryPerformanceState
} from '$lib/rb/performance-ipc.svelte';
export {
	gigRuntimeReleaseSettled,
	resetLibraryModeRuntimeForTest
} from '$lib/rb/library-mode-runtime';
