/**
 * One bundle, so the test sees the same pending load-play, feedback store and
 * stub IPC the installed listener uses. The test aliases
 * `$lib/rb/performance-ipc.svelte` to the stub below, so `sent` here is the
 * same array the listener's sends land in.
 */
export { installPerformanceHotkeys } from '$lib/rb/performance-hotkeys';
export {
	isNativeInteractiveTarget,
	POINTER_ONLY_HOTKEY_PASSTHROUGH_ATTR
} from '$lib/rb/performance-hotkeys-target';
export { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
export { beginPendingLoadPlay } from '$lib/rb/deck-slots';
export { feedbackState } from '$lib/rb/feedback-store.svelte';
export { sent } from './performance-hotkeys-waveform-focus-stub-ipc';
