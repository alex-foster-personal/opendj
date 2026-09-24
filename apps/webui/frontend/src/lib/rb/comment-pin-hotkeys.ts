import { isNativeInteractiveTarget } from '$lib/rb/performance-hotkeys-target';
import { armPinPlacement } from '$lib/rb/feedback-store.svelte';

/** Global `m` hotkey: arm comment-pin placement outside text fields. */
export function installCommentPinHotkeys(): () => void {
	const onKey = (e: KeyboardEvent): void => {
		if (e.key !== 'm' && e.key !== 'M') return;
		if (e.metaKey || e.ctrlKey || e.altKey) return;
		if (isNativeInteractiveTarget(e.target)) return;
		e.preventDefault();
		armPinPlacement();
	};
	window.addEventListener('keydown', onKey);
	return () => window.removeEventListener('keydown', onKey);
}
