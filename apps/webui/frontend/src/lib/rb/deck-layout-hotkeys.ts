/**
 * Cmd/Ctrl+2 / Cmd/Ctrl+4 - MORE/LESS two-deck performance layout (pin 862cd3).
 *
 * +2 = LESS (fewer decks visible, library gets the released room). +4 = MORE
 * (all four decks/mixer strips/wave rows visible). Suppressed while typing
 * in an input/textarea/select/contenteditable or while the settings overlay
 * is open - same gate as performance-hotkeys.ts and
 * technically-working-hotkeys.ts.
 *
 * These are chrome-only shortcuts: they never touch deck audio/transport,
 * only the persisted `deck_layout` preference that Mixer.svelte,
 * WaveformStack.svelte and +page.svelte read to decide what to hide. Decks
 * 3/4 stay mounted and keep playing/receiving IPC commands in LESS - only
 * their visual chrome collapses.
 *
 * `resolveDeckLayoutHotkeyMode` is split out as a pure function (no window/
 * document/prefs access) so the key-chord -> mode mapping and the typing/
 * settings-open suppression can be unit tested directly, without needing a
 * DOM or having to observe a mutation on a module instance the test cannot
 * see (this harness bundles each test's entry point in isolation - see
 * tests/unit/load-typescript.mjs).
 */
import {
	DECK_LAYOUT_LESS_KEY,
	DECK_LAYOUT_MORE_KEY
} from '$lib/components/rb/hotkeys/hotkeys-registry';
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { setDeckLayoutMode, type DeckLayoutMode } from '$lib/rb/prefs.svelte';
import { isSettingsOpen } from '$lib/settings/overlay.svelte';

export interface DeckLayoutHotkeyEvent {
	key: string;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	shiftKey: boolean;
	target: EventTarget | null;
}

function _typingTarget(t: EventTarget | null): boolean {
	return isTextEntryTarget(t);
}

function _isModChord(e: DeckLayoutHotkeyEvent, key: string): boolean {
	const mod = e.metaKey || e.ctrlKey;
	return mod && !e.altKey && !e.shiftKey && e.key === key;
}

/** Pure: given a key event and whether the settings overlay is open, returns
 * the deck_layout mode the chord requests, or null if the chord does not
 * apply / is suppressed. Never mutates prefs itself. */
export function resolveDeckLayoutHotkeyMode(
	e: DeckLayoutHotkeyEvent,
	opts: { settingsOpen: boolean }
): DeckLayoutMode | null {
	if (opts.settingsOpen || _typingTarget(e.target)) return null;
	if (_isModChord(e, DECK_LAYOUT_LESS_KEY)) return 'less';
	if (_isModChord(e, DECK_LAYOUT_MORE_KEY)) return 'more';
	return null;
}

export function installDeckLayoutHotkeys(): () => void {
	const onKey = (e: KeyboardEvent): void => {
		const mode = resolveDeckLayoutHotkeyMode(e, { settingsOpen: isSettingsOpen() });
		if (mode === null) return;
		e.preventDefault();
		setDeckLayoutMode(mode);
	};

	window.addEventListener('keydown', onKey);
	return () => window.removeEventListener('keydown', onKey);
}
