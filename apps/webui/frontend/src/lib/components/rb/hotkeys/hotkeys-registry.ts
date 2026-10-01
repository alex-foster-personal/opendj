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
 *     - install-hotkeys-overlay.ts imports HOTKEYS_OVERLAY_HOLD_KEY ('/')
 *       and HOTKEYS_OVERLAY_TOGGLE_KEY ('?') for the overlay's own gestures.
 *   DISPLAY-ONLY TRANSCRIPTION (handler logic stays put):
 *     - performance-hotkeys.ts (Space, Tab, +/-, ), m) -- transport/loop/pin
 *       state machine, latency-stamped Space, loop-target tracking.
 *     - technically-working-hotkeys.ts (Ctrl+R, Opt, Cmd+E) -- hold-vs-press
 *       plus pointer-edge hover, routed through performance IPC.
 *     - playlist-history-hotkeys.ts (Cmd+Z / Cmd+Shift+Z) -- shift-gated undo
 *       vs redo on one key, dispatched through performance IPC.
 *
 * This array is the single list the overlay renders. Do not hand-write a
 * second disconnected hotkey list in the Svelte component.
 */
import type { HotkeyEntry } from './hotkeys-filter';

export const DECK_LAYOUT_LESS_KEY = '2';
export const DECK_LAYOUT_MORE_KEY = '4';
export const DECK_LAYOUT_LESS_CHORD = 'Cmd+2';
export const DECK_LAYOUT_MORE_CHORD = 'Cmd+4';

export const SETTINGS_CHORD_KEY = ',';
export const SETTINGS_CHORD_CODE = 'Comma';
export const SETTINGS_CHORD_LABEL = 'Cmd+,';

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
		id: 'performance-space',
		chord: 'Space',
		description: 'Stop a playing library preview; otherwise toggle play/pause on the most recent deck',
		group: 'Performance'
	},
	{
		id: 'performance-tab',
		chord: 'Tab',
		description: 'Toggle library next-only filter (Camelot + BPM window)',
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
		id: 'performance-comment-pin',
		chord: 'M',
		description: 'Drop a comment pin without reaching for the topbar icon',
		group: 'Performance'
	},
	{
		id: 'playlist-undo',
		chord: 'Cmd+Z',
		description: 'Undo the last playlist edit',
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
	}
];
