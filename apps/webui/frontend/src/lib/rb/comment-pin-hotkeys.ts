import { armPinPlacement } from '$lib/rb/feedback-store.svelte';
import { resolveCommentPinHotkey } from '$lib/rb/comment-pin-hotkey-routing';

/** Global `m` / `Cmd+Shift+M` hotkey: arm comment-pin placement. */
export function installCommentPinHotkeys(): () => void {
	const onKey = (e: KeyboardEvent): void => {
		if (e.key !== 'm' && e.key !== 'M') return;
		if (resolveCommentPinHotkey(e) !== 'arm') return;
		e.preventDefault();
		armPinPlacement();
	};
	window.addEventListener('keydown', onKey);
	return () => window.removeEventListener('keydown', onKey);
}
