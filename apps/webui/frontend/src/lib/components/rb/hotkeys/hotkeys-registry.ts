/**
 * LIBUX-04 hotkeys overlay display registry.
 *
 * WHY A DISPLAY-SIDE TRANSCRIPTION PLUS A FEW IMPORTED CHORD CONSTANTS, NOT
 * "every handler imports its keys from here".
 *
 * The live handlers (performance-hotkeys.ts, technically-working-hotkeys.ts,
 * playlist-history-hotkeys.ts, deck-layout-hotkeys.ts, settings/hotkeys.ts)
 * encode bindings as conditionals inside a keydown handler, not as a
 * declarative list. They already have unit coverage that pins those
 * conditionals. Rewiring every handler to import its keys from this module
 * would be an invasive refactor of working, tested code -- including
 * state-machine-shaped modules (technically-working-hotkeys.ts,
 * performance-hotkeys.ts) whose "key" is not a single literal (Ctrl+R is
 * press=toggle AND hold=peek; Opt is hold-only; Space is latency-stamped).
 *
 * Split that this module actually lands:
 *   IMPORTED BY A HANDLER (single source of truth for the chord literal):
 *     - deck-layout-hotkeys.ts imports DECK_LAYOUT_LESS_KEY / MORE_KEY
 *       ('2' / '4') and uses them in resolveDeckLayoutHotkeyMode.
 *     - settings/hotkeys.ts imports SETTINGS_CHORD_KEY / SETTINGS_CHORD_CODE
 *       (',' / 'Comma') and uses them in the Cmd+, detector.
 *     - assistant/sidebar-toggle.ts imports ASSISTANT_CHORD_KEY / CODE
 *       ('i' / 'KeyI') for the Cmd+I assistant sidebar toggle.
 *     - install-hotkeys-overlay.ts imports HOTKEYS_OVERLAY_HOLD_KEY ('/')
 *       and HOTKEYS_OVERLAY_TOGGLE_KEY ('?') for the overlay's own gestures.
 *   DISPLAY-ONLY TRANSCRIPTION (handler logic stays put):
 *     - performance-hotkeys.ts (Space, +/-, ), m, , . Shift+K P) -- transport/loop/pin/tempo
 *       state machine, latency-stamped Space, loop-target tracking.
 *     - technically-working-hotkeys.ts (Ctrl+R, Opt, Cmd+E) -- hold-vs-press
 *       plus pointer-edge hover, routed through performance IPC.
 *     - playlist-history-hotkeys.ts (Cmd+Z / Cmd+Shift+Z) -- shift-gated undo
 *       vs redo on one key, dispatched through performance IPC.
 *     - browser-keyboard.ts (Shift+Left/Right load, Cmd+Shift+Left/Right
 *       instant double, Cmd+L locate) -- library deck chords (KEYB-11643).
 *     - set-notes/hotkey/right-cmd-tap.ts (Right Cmd / Right Ctrl tap) -- a
 *       press-release detector keyed on KeyboardEvent.code, not a chord.
 *     - commanded-keys.ts / knob-control.svelte.ts (1-9 glide, Cmd+1-9 jump) -- digit
 *       keys when a dial or fader is shift-selected.
 *
 * This array is the single list the overlay renders. Do not hand-write a
 * second disconnected hotkey list in the Svelte component.
 */
import type { CommandScope, KeyboardCommandDefinition } from '$lib/keyboard/command-registry';
import type { HotkeyEntry } from './hotkeys-filter';

export const DECK_LAYOUT_LESS_KEY = '2';
export const DECK_LAYOUT_MORE_KEY = '4';
export const DECK_LAYOUT_LESS_CHORD = 'Cmd+2';
export const DECK_LAYOUT_MORE_CHORD = 'Cmd+4';

export const SETTINGS_CHORD_KEY = ',';
export const SETTINGS_CHORD_CODE = 'Comma';
export const SETTINGS_CHORD_LABEL = 'Cmd+,';

export const ASSISTANT_CHORD_KEY = 'i';
export const ASSISTANT_CHORD_CODE = 'KeyI';
/** This browser's `navigator.platform`; the chord helpers take it so a test can name another. */
export const ASSISTANT_PLATFORM = globalThis.navigator?.platform ?? '';
/** Apple platforms use Cmd for the assistant chord; every other platform uses Ctrl. */
export const assistantChordUsesMeta = (platform: string): boolean =>
	/mac|iphone|ipad/i.test(platform);
export const assistantChordLabel = (platform: string): string =>
	assistantChordUsesMeta(platform) ? 'Cmd+I' : 'Ctrl+I';
export const assistantChordAria = (platform: string): string =>
	assistantChordUsesMeta(platform) ? 'Meta+I' : 'Control+I';
export const ASSISTANT_CHORD_LABEL = assistantChordLabel(ASSISTANT_PLATFORM);
export const ASSISTANT_CHORD_ARIA = assistantChordAria(ASSISTANT_PLATFORM);
export const SET_NOTES_TAP_CHORD = 'Right Cmd / Right Ctrl (tap)';

export const HOTKEYS_OVERLAY_HOLD_KEY = '/';
export const HOTKEYS_OVERLAY_TOGGLE_KEY = '?';
export const HOTKEYS_OVERLAY_HOLD_CHORD = '/';
export const HOTKEYS_OVERLAY_TOGGLE_CHORD = '?';
export const HOTKEYS_OVERLAY_CLOSE_CHORD = 'Esc / X';

export const HOTKEY_REGISTRY: readonly HotkeyEntry[] = [
	{
		id: 'hotkeys-hold',
		chord: HOTKEYS_OVERLAY_HOLD_CHORD,
		description: 'Hold to show the hotkeys overlay; release hides it',
		group: 'Hotkeys overlay'
	},
	{
		id: 'hotkeys-toggle',
		chord: HOTKEYS_OVERLAY_TOGGLE_CHORD,
		description: 'Toggle the hotkeys overlay on and off',
		group: 'Hotkeys overlay'
	},
	{
		id: 'hotkeys-close',
		chord: HOTKEYS_OVERLAY_CLOSE_CHORD,
		description: 'Hide the hotkeys overlay from either view and any filter',
		group: 'Hotkeys overlay'
	},
	{
		id: 'settings-open',
		chord: SETTINGS_CHORD_LABEL,
		description: 'Open the settings overlay',
		group: 'Settings'
	},
	{
		id: 'assistant-toggle',
		chord: ASSISTANT_CHORD_LABEL,
		description: 'Open or close the assistant sidebar (ignored while typing in a field)',
		group: 'Assistant'
	},
	{
		id: 'set-notes-toggle',
		chord: SET_NOTES_TAP_CHORD,
		description: 'Show or hide the Set notes pad (a chord does not)',
		group: 'Set notes'
	},
	{
		id: 'deck-layout-less',
		chord: DECK_LAYOUT_LESS_CHORD,
		description: 'LESS two-deck performance layout (library gets the released room)',
		group: 'Layout'
	},
	{
		id: 'deck-layout-more',
		chord: DECK_LAYOUT_MORE_CHORD,
		description: 'MORE four-deck performance layout (all decks/mixer strips/wave rows visible)',
		group: 'Layout'
	},
	{
		id: 'dial-position',
		chord: '1-9',
		description:
			'With a dial or fader selected: glide it to that position over 4 beats (1 = 0, 5 = centre, 9 = full)',
		group: 'Mixer'
	},
	{
		id: 'dial-jump',
		chord: 'Cmd+1-9',
		description:
			'With a dial or fader selected: jump it to that position now (otherwise Cmd+2 / Cmd+4 change the deck layout)',
		group: 'Mixer'
	},
	{
		id: 'performance-space',
		chord: 'Space',
		description:
			'Stop a playing library preview; otherwise toggle play/pause on the most recent deck',
		group: 'Performance'
	},
	{
		id: 'performance-loop-double',
		chord: '+',
		description: 'Double the last loop length',
		group: 'Performance'
	},
	{
		id: 'performance-loop-halve',
		chord: '-',
		description: 'Halve the last loop length',
		group: 'Performance'
	},
	{
		id: 'performance-loop-exit',
		chord: ')',
		description: 'Exit the last loop',
		group: 'Performance'
	},
	{
		id: 'performance-wave-zoom-out',
		chord: '[',
		description:
			'With edge sampler off: zoom the main waveform out one step. With edge sampler on: page the last-used edge set backward.',
		group: 'Performance'
	},
	{
		id: 'performance-wave-zoom-in',
		chord: ']',
		description:
			'With edge sampler off: zoom the main waveform in one step. With edge sampler on: page the last-used edge set forward.',
		group: 'Performance'
	},
	{
		id: 'edge-sampler-tab',
		chord: '1 .. 0',
		description: 'Fire tabs 1 through 10 on the last edge you used (Electron, sampler on)',
		group: 'Edge sampler'
	},
	{
		id: 'performance-tempo-fine',
		chord: ', .',
		description: "Step the recent deck's tempo down / up 0.1%",
		group: 'Performance'
	},
	{
		id: 'performance-tempo-coarse',
		chord: 'Shift+, .',
		description: "Step the recent deck's tempo down / up 1.0%",
		group: 'Performance'
	},
	{
		id: 'performance-tempo-reset',
		chord: 'Shift+K',
		description: "Reset the recent deck's tempo to 0%",
		group: 'Performance'
	},
	{
		id: 'performance-pitch-range-cycle',
		chord: 'P',
		description:
			"Cycle the recent deck's pitch range (8%, 16%, WIDE), skipping a range the current tempo does not fit",
		group: 'Performance'
	},
	{
		id: 'performance-comment-pin',
		chord: 'M',
		description: 'Drop a comment pin without reaching for the topbar icon',
		group: 'Performance'
	},
	{
		id: 'playlist-undo',
		chord: 'Cmd+Z',
		description: 'Undo the last playlist or tag change',
		group: 'Playlist'
	},
	{
		id: 'playlist-redo',
		chord: 'Cmd+Shift+Z',
		description: 'Redo the last playlist edit',
		group: 'Playlist'
	},
	{
		id: 'tech-mode-r',
		chord: 'Ctrl+R',
		description: 'Technically-working mode: press toggles overlay mode, hold peeks the full UI',
		group: 'Technically-working'
	},
	{
		id: 'tech-mode-opt',
		chord: 'Opt',
		description: 'Hold to reveal mixer/topbar/quick-draw over a translucent backdrop',
		group: 'Technically-working'
	},
	{
		id: 'tech-mode-eq',
		chord: 'Cmd+E',
		description: 'Toggle the floating EQ panel (technically-working overlay mode)',
		group: 'Technically-working'
	},
	{
		id: 'library-load-deck',
		chord: 'Shift+Left / Shift+Right',
		description: 'Load the selected track to deck 1 / deck 2',
		group: 'Library'
	},
	{
		id: 'library-instant-double',
		chord: 'Cmd+Shift+Left / Cmd+Shift+Right',
		description: 'Instant double: copy deck 2 onto deck 1 / deck 1 onto deck 2 (library focused)',
		group: 'Library'
	},
	{
		id: 'library-locate',
		chord: 'Cmd+L',
		description: "Select and scroll to the most recent deck's track in the library",
		group: 'Library'
	}
];

/** AISIDE-7892j: the assistant chord is listed only while its sidebar exists. */
export function availableHotkeys(
	registry: readonly HotkeyEntry[],
	assistantEnabled: boolean
): readonly HotkeyEntry[] {
	return assistantEnabled ? registry : registry.filter((entry) => entry.id !== 'assistant-toggle');
}

/** LIBUX-10456: the overlay group a registry command is listed under. */
function _commandGroup(id: string, scope: CommandScope): string {
	if (id.startsWith('preset.run.')) return 'Transition presets';
	switch (scope) {
		case 'global':
			return 'Performance';
		case 'deck':
			return 'Deck';
		case 'dial':
			return 'Dials';
		case 'fader':
			return 'Faders';
		case 'library':
			return 'Library';
		default: {
			const _exhaustive: never = scope;
			throw new Error(`Unhandled command scope: ${String(_exhaustive)}`);
		}
	}
}

/**
 * LIBUX-10456: one overlay row per performance command registry entry that has at
 * least one default key, so a registered command is never invisible in the
 * overlay; a command with no default key yields no (chord-less) row.
 */
export function hotkeyEntriesFromCommandRegistry(
	defs: readonly Pick<KeyboardCommandDefinition, 'id' | 'label' | 'defaultKeys' | 'scope'>[]
): HotkeyEntry[] {
	return defs
		.filter((def) => def.defaultKeys.length > 0)
		.map((def) => ({
			id: def.id,
			chord: def.defaultKeys.join(' / '),
			description: def.label,
			group: _commandGroup(def.id, def.scope)
		}));
}

/** The hand-written rows plus the registry's, one row per id: a registry row replaces a hand-written one. */
export function mergeHotkeyEntries(
	handWritten: readonly HotkeyEntry[],
	fromRegistry: readonly HotkeyEntry[]
): HotkeyEntry[] {
	const registryIds = new Set(fromRegistry.map((entry) => entry.id));
	return [...handWritten.filter((entry) => !registryIds.has(entry.id)), ...fromRegistry];
}
