/**
 * #12272: the hotkeys overlay as agents see and drive it.
 *
 * `queryPerformanceState().hotkeys_overlay_open` (and the same field in the UI
 * mirror that `opendj state --json` and MCP `app_state` return) reads the open
 * state, and the typed `{ type: 'hotkeys_overlay', open }` performance command
 * opens or closes it (`opendj hotkeys_overlay true|false`).
 *
 * Both go through `hotkeys-overlay.svelte.ts`, the one state machine the "?"
 * toggle, the "/" hold, Esc and the X button already drive, so the overlay an
 * agent opens is the overlay a person sees and can close with "?". The command
 * is UI state only: it has no deck, no queue scope and never reaches the engine.
 */
import {
	hideHotkeysOverlay,
	isHotkeysOverlayOpen,
	showHotkeysOverlay
} from '$lib/components/rb/hotkeys/hotkeys-overlay.svelte';

/** Open or close the hotkeys overlay on the shared overlay state machine. */
export function applyHotkeysOverlayCommand(open: boolean): void {
	if (open) {
		showHotkeysOverlay();
	} else if (!open) {
		hideHotkeysOverlay();
	}
}

/** Whether the hotkeys overlay is open, as the performance state reports it. */
export function hotkeysOverlayOpenSnapshot(): boolean {
	return isHotkeysOverlayOpen();
}
