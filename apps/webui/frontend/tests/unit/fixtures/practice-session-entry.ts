// One bundle for practice-session.test.mjs: the practice helper and the dispatcher it feeds must
// share a module instance, or the session it starts is invisible to the dispatcher under test.
export { installPracticeSession } from '$lib/components/rb/practice/practice-session';
export {
	activePerformanceCommandSession,
	dispatchPerformanceCommand,
	queryPerformanceState
} from '$lib/rb/performance-ipc.svelte';
