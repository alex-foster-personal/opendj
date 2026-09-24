/**
 * Window keydown/keyup wiring for the LIBUX-04 hotkeys overlay.
 *
 * "/" is hold-to-show (createHoldOrPress, hold-only, threshold 0).
 * "?" toggles the same overlay. Esc always hides, from either view and
 * any filter. Installed from root layout so it works on /performance
 * and the shell.
 *
 * Ignored while the settings overlay is open, and while focus is in an
 * input / textarea / select / contenteditable -- except the overlay's
 * own search box, which is a type-to-filter field, not a reason to
 * swallow the overlay gestures.
 */
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { isSettingsOpen } from '$lib/settings/overlay.svelte';
import {
	beginHotkeysOverlayHold,
	endHotkeysOverlayHold,
	getHotkeysOverlayQuery,
	hideHotkeysOverlay,
	isHotkeysOverlayOpen,
	resolveHotkeysOverlayHoldOnBlur,
	setHotkeysOverlayQuery,
	toggleHotkeysOverlay
} from './hotkeys-overlay.svelte';
import { HOTKEYS_OVERLAY_HOLD_KEY, HOTKEYS_OVERLAY_TOGGLE_KEY } from './hotkeys-registry';

const OVERLAY_SEARCH_ATTR = 'data-hotkeys-overlay-search';

function _typingTarget(t: EventTarget | null): boolean {
	return isTextEntryTarget(t);
}

function _isOverlaySearch(t: EventTarget | null): boolean {
	return t instanceof HTMLElement && t.hasAttribute(OVERLAY_SEARCH_ATTR);
}

function _isSlashHoldKey(e: KeyboardEvent): boolean {
	return (
		!e.metaKey &&
		!e.ctrlKey &&
		!e.altKey &&
		!e.shiftKey &&
		e.key === HOTKEYS_OVERLAY_HOLD_KEY
	);
}

function _isSlashKeyUp(e: KeyboardEvent): boolean {
	return e.key === HOTKEYS_OVERLAY_HOLD_KEY || e.code === 'Slash';
}

function _isToggleKey(e: KeyboardEvent): boolean {
	return !e.metaKey && !e.ctrlKey && !e.altKey && e.key === HOTKEYS_OVERLAY_TOGGLE_KEY;
}

function _isPrintable(e: KeyboardEvent): boolean {
	return e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey;
}

export function installHotkeysOverlayHotkeys(): () => void {
	const onKeyDown = (e: KeyboardEvent): void => {
		if (isSettingsOpen()) return;

		if (e.key === 'Escape' && isHotkeysOverlayOpen()) {
			e.preventDefault();
			e.stopPropagation();
			hideHotkeysOverlay();
			return;
		}

		const overlaySearch = _isOverlaySearch(e.target);
		const typing = _typingTarget(e.target) && !overlaySearch;

		if (_isSlashHoldKey(e)) {
			if (typing) return;
			e.preventDefault();
			e.stopPropagation();
			beginHotkeysOverlayHold();
			return;
		}

		if (_isToggleKey(e)) {
			if (typing) return;
			if (e.repeat) return;
			e.preventDefault();
			e.stopPropagation();
			toggleHotkeysOverlay();
			return;
		}

		if (!isHotkeysOverlayOpen() || overlaySearch) return;

		if (e.key === 'Backspace') {
			e.preventDefault();
			e.stopPropagation();
			setHotkeysOverlayQuery(getHotkeysOverlayQuery().slice(0, -1));
			return;
		}
		if (_isPrintable(e)) {
			e.preventDefault();
			e.stopPropagation();
			setHotkeysOverlayQuery(getHotkeysOverlayQuery() + e.key);
		}
	};

	const onKeyUp = (e: KeyboardEvent): void => {
		if (!_isSlashKeyUp(e)) return;
		endHotkeysOverlayHold();
	};

	const onBlur = (): void => {
		// A lost window focus (alt-tab, devtools, etc.) cannot deliver the
		// matching keyup. Threshold is 0, so down always means holding and
		// keyUp() is the resolving path -- same as Opt in
		// technically-working-hotkeys.ts. Without this the overlay sticks
		// open after alt-tabbing away while holding "/".
		resolveHotkeysOverlayHoldOnBlur();
	};

	window.addEventListener('keydown', onKeyDown, true);
	window.addEventListener('keyup', onKeyUp);
	window.addEventListener('blur', onBlur);
	return () => {
		window.removeEventListener('keydown', onKeyDown, true);
		window.removeEventListener('keyup', onKeyUp);
		window.removeEventListener('blur', onBlur);
	};
}
