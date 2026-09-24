/**
 * Loads the app-root comment-pin shell (the pin layer, the topbar pin button
 * and the global `m` hotkey) as one deferred boot task, off the first paint.
 *
 * It lives here rather than inline in +layout.svelte so the layout pays ONE
 * import edge for all of it: the quality ratchet counts dynamic `import()`
 * specifiers as edges, and the layout is already the frontend's widest
 * fan-out.
 *
 * A failed load is reported, never left as an absent shell. While the imports
 * were static a missing chunk failed the whole page; deferred, it would
 * otherwise leave an empty topbar slot with no pins, no button and no hotkey,
 * and nothing to say why. So `onError` receives the failure (the layout
 * raises an error toast and marks the slot), and it also receives an error
 * thrown by `onReady` itself, which is the same missing feature.
 */
import type { Component } from 'svelte';
import { bootScheduler, type BootScheduler } from '$lib/rb/boot-scheduler';

export interface FeedbackPinShell {
	layer: Component;
	shellButton: Component;
	installCommentPinHotkeys: () => () => void;
}

/** The real imports. Exported so a test can tell them apart from its own. */
export async function loadFeedbackPinShell(): Promise<FeedbackPinShell> {
	const [layer, shellButton, pinHotkeys] = await Promise.all([
		import('$lib/components/rb/FeedbackPinLayer.svelte'),
		import('$lib/components/rb/FeedbackPinShellButton.svelte'),
		import('$lib/rb/comment-pin-hotkeys')
	]);
	return {
		layer: layer.default,
		shellButton: shellButton.default,
		installCommentPinHotkeys: pinHotkeys.installCommentPinHotkeys
	};
}

/** Queue the load behind the boot window; `onReady` gets the loaded pieces,
 * `onError` gets the reason when they cannot be loaded or mounted. */
export function deferFeedbackPinShell(
	onReady: (shell: FeedbackPinShell) => void,
	onError: (error: unknown) => void,
	load: () => Promise<FeedbackPinShell> = loadFeedbackPinShell,
	scheduler: Pick<BootScheduler, 'defer'> = bootScheduler
): void {
	scheduler.defer('feedback-pin-layer:mount', () => {
		void load().then(onReady).catch(onError);
	});
}
