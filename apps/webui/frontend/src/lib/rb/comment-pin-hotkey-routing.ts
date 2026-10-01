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

/** A focused `<select>` counts as text entry for the shared shortcut gate
 * (Space opens it, letters type-ahead), but nobody TYPES prose into one, so
 * the comment hotkey claims `m` there (pin 28a5effd). */
function _isSelect(target: EventTarget | null): boolean {
	return (
		target !== null &&
		typeof target === 'object' &&
		(target as { tagName?: string }).tagName === 'SELECT'
	);
}

/**
 * Pure routing for arming comment-pin placement via keyboard.
 *
 * There is deliberately no Settings veto (pin 28a5effd): pins are placed on
 * the Settings overlay like on any other surface, and the placement layer
 * stacks above it. The only thing that keeps plain `m` from arming is real
 * text entry: a text input, a textarea, or a contenteditable.
 */
export function resolveCommentPinHotkey(e: CommentPinKeyboardEventLike): CommentPinHotkeyAction {
	if (e.key !== 'm' && e.key !== 'M') return null;
	const backup = (e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey;
	if (backup) return 'arm';
	if (e.metaKey || e.ctrlKey || e.altKey) return null;
	if (_isSelect(e.target)) return 'arm';
	if (isTextEntryTarget(e.target)) {
		return _pointerOnlyPassthrough(e.target) ? 'arm' : null;
	}
	return 'arm';
}
