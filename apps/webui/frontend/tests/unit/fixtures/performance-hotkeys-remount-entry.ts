/**
 * One bundle, so the test arms the same pending load-play the hotkey listener
 * reads. `sent` comes from the stub by its own path; the test aliases
 * `$lib/rb/performance-ipc.svelte` to that same file, so both are one module.
 */
export { installPerformanceHotkeys } from '$lib/rb/performance-hotkeys';
export { beginPendingLoadPlay, clearPendingLoadPlay } from '$lib/rb/deck-slots';
export { sent } from './performance-hotkeys-remount-stub-ipc';
