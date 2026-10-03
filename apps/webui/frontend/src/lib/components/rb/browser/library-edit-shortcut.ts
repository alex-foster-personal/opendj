/**
 * Which library edit shortcut (Cmd/Ctrl+A, C, X, V) a key press asks for.
 * Split out of ./track-clipboard so BrowserPanel can decide synchronously
 * whether to take the key while the clipboard rules load on first use.
 */

import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';

export type LibraryEditShortcut = 'select_all' | 'copy' | 'cut' | 'paste';

export type ShortcutKeyEvent = {
	key: string;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	shiftKey: boolean;
	target: EventTarget | null;
};

const SHORTCUT_KEYS: Record<string, LibraryEditShortcut> = {
	a: 'select_all',
	c: 'copy',
	x: 'cut',
	v: 'paste'
};

/** The library shortcut this key press asks for, or null when it is not one
 * or belongs to a text field (where Cmd+A/C/V must keep editing text). */
export function libraryEditShortcut(e: ShortcutKeyEvent): LibraryEditShortcut | null {
	if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return null;
	if (isTextEntryTarget(e.target)) return null;
	return SHORTCUT_KEYS[e.key.toLowerCase()] ?? null;
}
