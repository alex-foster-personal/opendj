/**
 * One bundle, so the test sees the pending load-play and the stub IPC the
 * listener uses. The test aliases `$lib/rb/performance-ipc.svelte` to the stub
 * below, so `sent` here is the same array the listener's sends land in.
 */
export { installPerformanceHotkeys } from '$lib/rb/performance-hotkeys';
export { beginPendingLoadPlay } from '$lib/rb/deck-slots';
export { sent } from './performance-native-space-stub-ipc';
