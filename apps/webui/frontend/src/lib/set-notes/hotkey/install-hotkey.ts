/**
 * SETNOTE-6340b window wiring: a lone right-Cmd / right-Ctrl tap toggles the pad.
 *
 * Installed from the root layout so it works on /performance too. Taps are
 * ignored while the settings overlay is open. A tap inside a text field still
 * toggles: it types nothing, so there is no keystroke to protect, and it is
 * how the pad is dismissed from its own editor.
 */
import { isSettingsOpen } from '$lib/settings/overlay.svelte';
import { createRightCmdTapDetector } from './right-cmd-tap';

export function installSetNotesHotkey(toggle: () => void): () => void {
	const detector = createRightCmdTapDetector();
	const onKeyDown = (e: KeyboardEvent): void => detector.keyDown(e);
	const onKeyUp = (e: KeyboardEvent): void => {
		if (detector.keyUp(e) && !isSettingsOpen()) toggle();
	};
	const onBlur = (): void => detector.blur();

	window.addEventListener('keydown', onKeyDown, true);
	window.addEventListener('keyup', onKeyUp, true);
	window.addEventListener('blur', onBlur);
	return () => {
		window.removeEventListener('keydown', onKeyDown, true);
		window.removeEventListener('keyup', onKeyUp, true);
		window.removeEventListener('blur', onBlur);
	};
}
