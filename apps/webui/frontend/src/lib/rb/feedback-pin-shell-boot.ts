/**
 * Loads the app-root comment-pin shell (the pin layer, the topbar pin button
 * and the global `m` hotkey) as one deferred boot task, off the first paint.
 *
 * It lives here rather than inline in +layout.svelte so the layout pays ONE
 * import edge for all of it: the quality ratchet counts dynamic `import()`
 * specifiers as edges, and the layout is already the frontend's widest
 * fan-out.
 */
import type { Component } from 'svelte';
import { bootScheduler } from '$lib/rb/boot-scheduler';

export interface FeedbackPinShell {
	layer: Component;
	shellButton: Component;
	installCommentPinHotkeys: () => () => void;
}

/** Queue the load behind the boot window; `onReady` gets the loaded pieces. */
export function deferFeedbackPinShell(onReady: (shell: FeedbackPinShell) => void): void {
	bootScheduler.defer('feedback-pin-layer:mount', () =>
		void Promise.all([
			import('$lib/components/rb/FeedbackPinLayer.svelte'),
			import('$lib/components/rb/FeedbackPinShellButton.svelte'),
			import('$lib/rb/comment-pin-hotkeys')
		]).then(([layer, shellButton, pinHotkeys]) =>
			onReady({
				layer: layer.default,
				shellButton: shellButton.default,
				installCommentPinHotkeys: pinHotkeys.installCommentPinHotkeys
			})
		)
	);
}
