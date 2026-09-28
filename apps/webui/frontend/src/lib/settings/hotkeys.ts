/**
 * Global Cmd+, (Ctrl+, on non-Mac) to open the settings overlay, plus Escape
 * to close it. Installed from root layout so it works on /performance and
 * the shell.
 *
 * review r3549 P2: this file has no bare-single-key branch, so it must NOT
 * gate itself on isTextEntryTarget the way the single-key shortcut modules
 * do (that predicate exists to stop a plain letter/Space from firing while
 * typing). A modifier chord types nothing, and Escape has no text-editing
 * meaning either, so both must keep working even while focus is inside a
 * text field in the settings overlay - otherwise a user who clicks into a
 * search box there has no keyboard way out.
 */
import {
	SETTINGS_CHORD_CODE,
	SETTINGS_CHORD_KEY
} from '$lib/components/rb/hotkeys/hotkeys-registry';
import { closeSettings, isSettingsOpen, openSettings, toggleSettings } from './overlay.svelte';

function _isSettingsChord(e: KeyboardEvent): boolean {
	const mod = e.metaKey || e.ctrlKey;
	return (
		mod && !e.altKey && !e.shiftKey && (e.key === SETTINGS_CHORD_KEY || e.code === SETTINGS_CHORD_CODE)
	);
}

export function installSettingsHotkeys(): () => void {
	const onKey = (e: KeyboardEvent): void => {
		if (_isSettingsChord(e)) {
			e.preventDefault();
			e.stopPropagation();
			toggleSettings();
			return;
		}
		if (e.key === 'Escape' && isSettingsOpen()) {
			e.preventDefault();
			closeSettings();
		}
	};
	window.addEventListener('keydown', onKey, true);
	return () => window.removeEventListener('keydown', onKey, true);
}

export { openSettings, closeSettings, toggleSettings };
