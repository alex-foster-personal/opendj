import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { POINTER_ONLY_HOTKEY_PASSTHROUGH_ATTR } from '$lib/rb/performance-hotkeys-target';

export type CommentPinHotkeyAction = 'arm' | null;

export interface CommentPinKeyboardEventLike {
	key: string;
	target: EventTarget | null;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	shiftKey: boolean;
}

function _pointerOnlyPassthrough(target: EventTarget | null): boolean {
	return target instanceof Element && target.hasAttribute(POINTER_ONLY_HOTKEY_PASSTHROUGH_ATTR);
}

/** Pure routing for arming comment-pin placement via keyboard. */
export function resolveCommentPinHotkey(
	e: CommentPinKeyboardEventLike,
	options: { settingsOpen?: boolean } = {}
): CommentPinHotkeyAction {
	if (options.settingsOpen === true) return null;
	if (e.key !== 'm' && e.key !== 'M') return null;
	const backup = (e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey;
	if (backup) return 'arm';
	if (e.metaKey || e.ctrlKey || e.altKey) return null;
	if (isTextEntryTarget(e.target)) {
		return _pointerOnlyPassthrough(e.target) ? 'arm' : null;
	}
	return 'arm';
}
