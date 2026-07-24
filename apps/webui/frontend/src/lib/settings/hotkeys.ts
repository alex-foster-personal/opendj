/**
 * Global Cmd+, (Ctrl+, on non-Mac) to open the settings overlay.
 * Installed from root layout so it works on /performance and the shell.
 * Performance hotkeys already ignore meta/ctrl, so they do not conflict.
 */
import { closeSettings, isSettingsOpen, openSettings, toggleSettings } from './overlay.svelte';

function _isSettingsChord(e: KeyboardEvent): boolean {
	const mod = e.metaKey || e.ctrlKey;
	return mod && !e.altKey && !e.shiftKey && (e.key === ',' || e.code === 'Comma');
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
