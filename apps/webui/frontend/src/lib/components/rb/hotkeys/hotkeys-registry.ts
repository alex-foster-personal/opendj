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
 *     - performance-hotkeys.ts (Space, +/-, ), m, C, V, K, `, , . Shift+K P, 1-8 hot cues) -- transport/loop/pin/deck/tempo
 *       state machine, latency-stamped Space, loop-target tracking. #12270:
 *       tests/unit/hotkeys-overlay-bindings.test.mjs presses these rows through the router.
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
/** Apple platforms use Cmd for the primary modifier; every other platform uses Ctrl. */
export const platformUsesMeta = (platform: string): boolean => /mac|iphone|ipad/i.test(platform);
/** The assistant chord's modifier test, the same platform check as every overlay label. */
export const assistantChordUsesMeta = platformUsesMeta;

// #12282: chord labels are written once with "Cmd" for the primary modifier
// (Cmd on Apple, Ctrl elsewhere, as keymap.ts reads it); only "Cmd" followed
// by "+" is a modifier, so "Right Cmd" (a key name) is left alone.
const PRIMARY_MODIFIER_LABEL = /\bCmd(?=\+)/g;

/** A chord label as `platform` shows it: Cmd on Apple platforms, Ctrl everywhere else. */
export function platformChordLabel(label: string, platform: string): string {
	return platformUsesMeta(platform) ? label : label.replace(PRIMARY_MODIFIER_LABEL, 'Ctrl');
}

// WAI-ARIA aria-keyshortcuts modifier names; "Cmd" resolves per platform.
const ARIA_MODIFIER: Readonly<Record<string, string>> = {
	Ctrl: 'Control',
	Opt: 'Alt',
	Alt: 'Alt',
	Shift: 'Shift'
};

/** The aria-keyshortcuts value for a chord label: Cmd is Meta on Apple, Control elsewhere; "A / B" lists both. */
export function platformChordAria(label: string, platform: string): string {
	const primary = platformUsesMeta(platform) ? 'Meta' : 'Control';
	const ariaChord = (chord: string): string => {
		const parts = chord.split('+');
		const key = parts.pop() ?? '';
		const modifiers = parts.map((part) =>
			part === 'Cmd' ? primary : (ARIA_MODIFIER[part] ?? part)
		);
		return [...modifiers, key].join('+');
	};
	return label.split(' / ').map(ariaChord).join(' ');
}

/** Overlay rows as `platform` shows them: chord and description name that platform's modifier. */
export function platformHotkeyEntries(
	entries: readonly HotkeyEntry[],
	platform: string
): HotkeyEntry[] {
	return entries.map((entry) => ({
		...entry,
		chord: platformChordLabel(entry.chord, platform),
		description: platformChordLabel(entry.description, platform)
	}));
}

export const assistantChordLabel = (platform: string): string =>
	platformChordLabel('Cmd+I', platform);
export const assistantChordAria = (platform: string): string =>
	platformChordAria('Cmd+I', platform);
export const ASSISTANT_CHORD_LABEL = assistantChordLabel(ASSISTANT_PLATFORM);
export const ASSISTANT_CHORD_ARIA = assistantChordAria(ASSISTANT_PLATFORM);
export const SET_NOTES_TAP_CHORD = 'Right Cmd / Right Ctrl (tap)';

export const HOTKEYS_OVERLAY_HOLD_KEY = '/';
export const HOTKEYS_OVERLAY_TOGGLE_KEY = '?';
export const HOTKEYS_OVERLAY_HOLD_CHORD = '/';
export const HOTKEYS_OVERLAY_TOGGLE_CHORD = '?';
export const HOTKEYS_OVERLAY_CLOSE_CHORD = 'Esc / X';

// aria-keyshortcuts names the keys pressed, not the character they make
// (MDN: "@" on a US keyboard is "Shift+2"), so "?" is announced as "Shift+/".
const US_SHIFTED_BASE_KEY: Readonly<Record<string, string>> = {
	'!': '1',
	'@': '2',
	'#': '3',
	$: '4',
	'%': '5',
	'^': '6',
	'&': '7',
	'*': '8',
	'(': '9',
	')': '0',
	_: '-',
	'+': '=',
	'{': '[',
	'}': ']',
	'|': '\\',
	':': ';',
	'"': "'",
	'<': ',',
	'>': '.',
	'?': '/',
	'~': '`'
};

/** The aria-keyshortcuts value for one unmodified key, as a US layout presses it. */
export function keyToAriaShortcut(key: string): string {
	const base = US_SHIFTED_BASE_KEY[key];
	return base === undefined ? key : `Shift+${base}`;
}

/** What the overlay's own toggle control (#12277) announces: its name and the live "?" and Esc keys. */
export interface HotkeysOverlayTrigger {
	label: string;
	title: string;
	ariaKeyshortcuts: string;
}

/** Trigger metadata built from a toggle key, so the announced shortcut follows the installer's constant. */
export function hotkeysOverlayTrigger(toggleKey: string): HotkeysOverlayTrigger {
	return {
		label: 'Close hotkeys overlay',
		title: `Hide hotkeys overlay (Esc or ${toggleKey})`,
		ariaKeyshortcuts: `Escape ${keyToAriaShortcut(toggleKey)}`
	};
}

export const HOTKEYS_OVERLAY_TRIGGER = hotkeysOverlayTrigger(HOTKEYS_OVERLAY_TOGGLE_KEY);

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
		description: 'Clear the filter, or hide the hotkeys overlay when it is empty',
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
		chord: 'Space / Cmd+Space',
		description:
			'Stop a playing library preview; otherwise toggle play/pause on the most recent deck (Cmd+Space starts it on the quantize grid)',
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
		description: 'Zoom the main waveform out one step',
		group: 'Performance'
	},
	{
		id: 'performance-wave-zoom-in',
		chord: ']',
		description: 'Zoom the main waveform in one step',
		group: 'Performance'
	},
	{
		id: 'performance-section-jump-next',
		chord: 'Shift+]',
		description: 'Jump the most recent deck to the next analysed section start, on its downbeat',
		group: 'Performance'
	},
	{
		id: 'performance-section-jump-previous',
		chord: 'Shift+[',
		description:
			'Jump the most recent deck to the previous section start (press again within 1 s to go back one more)',
		group: 'Performance'
	},
	{
		id: 'edge-sampler-tab',
		chord: '1 .. 0',
		description: 'Fire tabs 1 through 10 on the awake edge (Electron, sampler on, edge awake)',
		group: 'Edge sampler'
	},
	{
		id: 'edge-sampler-page',
		chord: 'PageUp / PageDown',
		description: "Page the awake edge's set back / forward (Electron, sampler on, edge awake)",
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
		chord: 'M / Cmd+Shift+M',
		description:
			'Drop a comment pin without reaching for the topbar icon (Cmd+Shift+M also works in a text field)',
		group: 'Performance'
	},
	{
		id: 'performance-cue',
		chord: 'C',
		description: 'Cue the keyboard deck (the deck whose header carries the marker)',
		group: 'Performance'
	},
	{
		id: 'performance-beat-sync',
		chord: 'V',
		description: 'Toggle beat sync on the keyboard deck',
		group: 'Performance'
	},
	{
		id: 'performance-master-tempo',
		chord: 'K',
		description: 'Toggle master tempo (key lock) on the keyboard deck',
		group: 'Performance'
	},
	{
		id: 'performance-cycle-deck',
		chord: '` / Shift+`',
		description: 'Make the next loaded deck the keyboard deck (Shift goes back)',
		group: 'Performance'
	},
	{
		id: 'performance-hot-cue-trigger',
		chord: '1-8',
		description:
			'Jump to hot cue 1-8 (A-H) on the most recent deck; ignored while a dial or fader is selected',
		group: 'Performance'
	},
	{
		id: 'performance-hot-cue-save',
		chord: 'Shift+1-8',
		description:
			'Save the playhead into an empty hot cue slot on the most recent deck (a filled slot is never overwritten)',
		group: 'Performance'
	},
	{
		id: 'performance-hot-cue-clear',
		chord: 'Cmd+Shift+1-8',
		description: 'Clear hot cue 1-8 on the most recent deck',
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
		id: 'large-library',
		chord: 'Right Opt',
		description: 'Toggle the large library (library grows, wavestack and decks at their floors)',
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

/** #12254: the overlay group that names the keyboard selector's keys. */
export const SELECTOR_CONTROLS_GROUP = 'Selector controls';

/**
 * #12254: the keyboard selector's keys (#9395 binds W A S D / E / Q / Esc, #13708 Enter and the
 * arrows). Ids and chords match the selector.* command registry entries, so once those carry
 * default keys their registry rows replace these one for one instead of listing a chord twice.
 */
export const SELECTOR_CONTROL_HOTKEYS: readonly HotkeyEntry[] = [
	...(
		[
			['up', 'W'],
			['left', 'A'],
			['down', 'S'],
			['right', 'D']
		] as const
	).map(([direction, chord]) => ({
		id: `selector.move.${direction}`,
		chord,
		description: `Move selector ${direction}`,
		group: SELECTOR_CONTROLS_GROUP
	})),
	{
		id: 'selector.drill-in',
		chord: 'E',
		description: 'Drill into selector',
		group: SELECTOR_CONTROLS_GROUP
	},
	{
		id: 'selector.drill-out',
		chord: 'Q',
		description: 'Drill out of selector',
		group: SELECTOR_CONTROLS_GROUP
	},
	{
		id: 'selector-clear',
		chord: 'Esc',
		description: 'Clear the selector selection and hide its ring',
		group: SELECTOR_CONTROLS_GROUP
	},
	{
		id: 'selector.activate',
		chord: 'Enter',
		description: 'Activate selected control',
		group: SELECTOR_CONTROLS_GROUP
	},
	{
		id: 'selector.adjust.up',
		chord: 'ArrowUp / ArrowRight',
		description: 'Adjust selected control up',
		group: SELECTOR_CONTROLS_GROUP
	},
	{
		id: 'selector.adjust.down',
		chord: 'ArrowDown / ArrowLeft',
		description: 'Adjust selected control down',
		group: SELECTOR_CONTROLS_GROUP
	}
];

/**
 * #12287: the build flag (apps/feature_flags/store.py FLAGS) each flag-gated row needs; a row
 * not named here always works. The CLI export carries it, so `opendj hotkeys` can mark the row.
 */
export const HOTKEY_FEATURE_FLAGS: Readonly<Record<string, string>> = {
	'assistant-toggle': 'assistant.sidebar'
};

/** AISIDE-7892j: the assistant chord is listed only while its sidebar exists. */
export function availableHotkeys(
	registry: readonly HotkeyEntry[],
	assistantEnabled: boolean
): readonly HotkeyEntry[] {
	return assistantEnabled
		? registry
		: registry.filter((entry) => HOTKEY_FEATURE_FLAGS[entry.id] === undefined);
}

/** #12287: the platforms the CLI contract renders, each named by a `navigator.platform` value. */
export const HOTKEY_CONTRACT_PLATFORMS = { mac: 'MacIntel', other: 'Win32' } as const;

/** One overlay row as the CLI contract carries it: rendered per platform, plus its gating flag. */
export interface HotkeyContractRow {
	id: string;
	group: string;
	feature_flag: string | null;
	mac: { chord: string; description: string };
	other: { chord: string; description: string };
}

/** #12287: overlay rows rendered for every contract platform by the overlay's own formatter. */
export function hotkeyContractRows(entries: readonly HotkeyEntry[]): HotkeyContractRow[] {
	const mac = platformHotkeyEntries(entries, HOTKEY_CONTRACT_PLATFORMS.mac);
	const other = platformHotkeyEntries(entries, HOTKEY_CONTRACT_PLATFORMS.other);
	return entries.map((entry, index) => ({
		id: entry.id,
		group: entry.group,
		feature_flag: HOTKEY_FEATURE_FLAGS[entry.id] ?? null,
		mac: { chord: mac[index].chord, description: mac[index].description },
		other: { chord: other[index].chord, description: other[index].description }
	}));
}

/** LIBUX-10456: the overlay group a registry command is listed under. */
function _commandGroup(id: string, scope: CommandScope): string {
	if (id.startsWith('preset.run.')) return 'Transition presets';
	if (id.startsWith('selector.')) return SELECTOR_CONTROLS_GROUP;
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

/** The hand-written rows plus the registry's, one row per id: a registry row replaces a hand-written one in place. */
export function mergeHotkeyEntries(
	handWritten: readonly HotkeyEntry[],
	fromRegistry: readonly HotkeyEntry[]
): HotkeyEntry[] {
	const registryById = new Map(fromRegistry.map((entry) => [entry.id, entry]));
	const handIds = new Set(handWritten.map((entry) => entry.id));
	return [
		...handWritten.map((entry) => registryById.get(entry.id) ?? entry),
		...fromRegistry.filter((entry) => !handIds.has(entry.id))
	];
}

/** #12254: every row the overlay lists: the hand-written rows, the selector controls, then registry commands. */
export function overlayHotkeyEntries(
	defs: readonly Pick<KeyboardCommandDefinition, 'id' | 'label' | 'defaultKeys' | 'scope'>[]
): HotkeyEntry[] {
	return mergeHotkeyEntries(
		[...HOTKEY_REGISTRY, ...SELECTOR_CONTROL_HOTKEYS],
		hotkeyEntriesFromCommandRegistry(defs)
	);
}
