import { armPinPlacement } from '$lib/rb/feedback-store.svelte';
import { resolveCommentPinHotkey } from '$lib/rb/comment-pin-hotkey-routing';
import { isSettingsOpen } from '$lib/settings/overlay.svelte';

/** Global `m` / `Cmd+Shift+M` hotkey: arm comment-pin placement. */
export function installCommentPinHotkeys(): () => void {
	const onKey = (e: KeyboardEvent): void => {
		if (e.key !== 'm' && e.key !== 'M') return;
		if (resolveCommentPinHotkey(e, { settingsOpen: isSettingsOpen() }) !== 'arm') return;
		e.preventDefault();
		armPinPlacement();
	};
	window.addEventListener('keydown', onKey);
	return () => window.removeEventListener('keydown', onKey);
}
