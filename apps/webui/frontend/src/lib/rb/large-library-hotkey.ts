/**
 * Issue #9432: right Option (KeyboardEvent.code 'AltRight') alone, outside text
 * entry, toggles the large library through the toggle_large_library
 * performance command. Left Option keeps its technically-working reveal.
 */
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';

export interface LargeLibraryHotkeyEvent {
	code: string;
	metaKey: boolean;
	ctrlKey: boolean;
	shiftKey: boolean;
	repeat?: boolean;
	target: EventTarget | null;
}

export function isLargeLibraryHotkey(e: LargeLibraryHotkeyEvent): boolean {
	if (e.code !== 'AltRight' || e.repeat) return false;
	if (e.metaKey || e.ctrlKey || e.shiftKey) return false;
	return !isTextEntryTarget(e.target);
}

export function installLargeLibraryHotkey(dispatch: () => void): () => void {
	const onKey = (e: KeyboardEvent): void => {
		if (!isLargeLibraryHotkey(e)) return;
		e.preventDefault();
		dispatch();
	};
	window.addEventListener('keydown', onKey);
	return () => window.removeEventListener('keydown', onKey);
}
