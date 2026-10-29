/**
 * Window wiring for the R8 Ctrl-hold STEM gesture (logic in stem-hold.ts).
 * Every mode change goes through `sendStemEqMode`, which the /performance page
 * wires to runPerformanceCommandFromUi with the same `stem_eq_mode` command the
 * channel STEM button sends, so agents already have the per-deck equivalent and
 * a preset lock rejects it the same way. Injected rather than imported so this
 * module adds no importer to performance-ipc (fan-in is ratcheted).
 */
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { DECK_IDS } from '$lib/player/constants';
import { getDeckState, mixerState } from '$lib/player/state.svelte';
import { isSettingsOpen } from '$lib/settings/overlay.svelte';
import { createHeldKeys, createStemHold } from './stem-hold';

function _isCtrlAlone(e: KeyboardEvent): boolean {
	return e.key === 'Control' && !e.metaKey && !e.altKey && !e.shiftKey;
}

/** Resolves true only when the command ran (runPerformanceCommandFromUi's `ok`). */
export type SendStemEqMode = (deck: (typeof DECK_IDS)[number], enabled: boolean) => Promise<boolean>;

export function installStemHoldHotkey(sendStemEqMode: SendStemEqMode): () => void {
	const hold = createStemHold({
		decks: DECK_IDS,
		stemsReady: (d) => getDeckState(d).stems.status === 'ready',
		stemEqMode: (d) => mixerState.channels[d].stem_eq_mode,
		setStemEqMode: (deck, enabled) => sendStemEqMode(deck, enabled),
		// The refusal itself is already toasted by the command path; this
		// line names what it left behind. The next Ctrl hold retries it.
		reportRestoreRefused: (deck, cause) =>
			console.error(`[stem-hold] deck ${deck} stayed in STEM: switching it back was refused`, cause),
		reportEnableFailed: (deck, cause) =>
			console.error(`[stem-hold] deck ${deck} not switched to STEM: the command failed`, cause),
		setTimer: (fn, ms) => setTimeout(fn, ms),
		clearTimer: (h) => clearTimeout(h as ReturnType<typeof setTimeout>)
	});

	const heldKeys = createHeldKeys();

	const onKeyDown = (e: KeyboardEvent): void => {
		if (_isCtrlAlone(e)) {
			if (e.repeat || heldKeys.othersHeld || isSettingsOpen() || isTextEntryTarget(e.target)) return;
			hold.ctrlDown();
			return;
		}
		// Ctrl+Z, Ctrl+R, any other key: never a stem hold.
		if (e.key !== 'Control') heldKeys.down(e.code);
		hold.release();
	};
	const onKeyUp = (e: KeyboardEvent): void => {
		if (e.key === 'Control') hold.release();
		else heldKeys.up(e.code);
	};
	const onPointerDown = (): void => hold.release();
	const onBlur = (): void => {
		heldKeys.clear();
		hold.release();
	};
	const onVisibility = (): void => {
		if (document.visibilityState !== 'hidden') return;
		heldKeys.clear();
		hold.release();
	};

	window.addEventListener('keydown', onKeyDown, { capture: true });
	window.addEventListener('keyup', onKeyUp, { capture: true });
	window.addEventListener('pointerdown', onPointerDown, { capture: true });
	window.addEventListener('blur', onBlur);
	document.addEventListener('visibilitychange', onVisibility);
	return () => {
		hold.release();
		window.removeEventListener('keydown', onKeyDown, { capture: true });
		window.removeEventListener('keyup', onKeyUp, { capture: true });
		window.removeEventListener('pointerdown', onPointerDown, { capture: true });
		window.removeEventListener('blur', onBlur);
		document.removeEventListener('visibilitychange', onVisibility);
	};
}
