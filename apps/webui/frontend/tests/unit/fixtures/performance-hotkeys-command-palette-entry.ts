/**
 * One bundle for the Cmd-K coexistence tests (#12279), so the test sees the
 * same pending load-play, comment-pin state and stub IPC the listener uses.
 * The test aliases `$lib/rb/performance-ipc.svelte` to the stub below.
 */
export { installPerformanceHotkeys } from '$lib/rb/performance-hotkeys';
export { beginPendingLoadPlay } from '$lib/rb/deck-slots';
export { feedbackState } from '$lib/rb/feedback-store.svelte';
export { sent } from './performance-hotkeys-command-palette-stub-ipc';
