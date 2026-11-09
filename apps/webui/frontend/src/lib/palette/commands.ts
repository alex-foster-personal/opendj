/**
 * CMDK-9600: one palette command registry for the Cmd K v2 bar.
 *
 * Pure data (no Svelte imports) so node unit tests can load it. Titles,
 * keywords and argument schema are authored here; IPC route labels come from
 * `MENU_COMMANDS` via `describeCommand`, and shortcut hints from
 * `HOTKEY_REGISTRY`. Entries with `run: null` stay registered but are not
 * offered as bar rows until argument prompts (#9466) or bar wiring lands.
 */
import { describeCommand, MENU_COMMANDS, type MenuCommandId } from '$lib/context-menu/commands';
import { HOTKEY_REGISTRY } from '$lib/components/rb/hotkeys/hotkeys-registry';
import type { DeckId } from '$lib/rb/deck-id';

/** Argument kinds the issue names; value prompts are #9466. */
type PaletteArg =
	| { kind: 'deck' }
	| { kind: 'cue_slot' }
	| { kind: 'beats' }
	| { kind: 'value'; name: string; min?: number; max?: number; options?: readonly string[] };

/** The deck fields palette predicates and builders read (superset of cmdk-actions ActionDeck). */
export interface PaletteDeck {
	id: DeckId;
	loaded: boolean;
	playing: boolean;
	is_master: boolean;
	beat_sync_enabled: boolean;
	load_generation: number;
	title: string | null;
}

export interface PaletteContext {
	target: DeckId;
	decks: readonly PaletteDeck[];
}

/** PerformanceCommand members the bar may send through the page's runCommand prop. */
export type PaletteIpcCommand =
	| { type: 'play'; deck: DeckId; playing: boolean }
	| { type: 'loop'; deck: DeckId; loop: null }
	| { type: 'master'; deck: DeckId }
	| { type: 'beat_sync'; deck: DeckId; enabled: boolean }
	| { type: 'playlist_undo' }
	| { type: 'playlist_redo' }
	| { type: 'tech_mode_toggle' }
	| { type: 'wave_zoom_step'; direction: 'in' | 'out' };

export type PaletteBarRun =
	{ via: 'ipc'; command: PaletteIpcCommand } | { via: 'ui'; action: 'show_hotkeys' };

type PaletteRunner =
	| { kind: 'builtin'; row: 'eject' | 'open_settings' }
	| { kind: 'bar'; build: (ctx: PaletteContext, deck: PaletteDeck | null) => PaletteBarRun }
	| null;

interface PaletteCommand {
	id: string;
	title: string;
	keywords: readonly string[];
	args: readonly PaletteArg[];
	menu: MenuCommandId | null;
	hotkey: string | null;
	shortcut: string | null;
	route: string | null;
	run: PaletteRunner;
	waits?: string;
	available: (ctx: PaletteContext) => boolean;
}

type PaletteCommandSpec = Omit<PaletteCommand, 'shortcut' | 'route'>;

const NEEDS_TRACK =
	'Acts on selected track rows; reached from the track right-click menu until argument prompts (#9466) can name a track.';
const NEEDS_LIST =
	'Acts on a playlist, section or smartlist row; reached from the tree right-click menu until argument prompts (#9466).';
const NEEDS_VOLUME = 'Acts on a mounted USB volume; reached from the USB stick menu.';
const NEEDS_VALUE =
	'Sets a mixer control to a value; reached from the dial and channel right-click menus until argument prompts (#9466) can take a value.';
const NEEDS_STEM =
	'Acts on one stem lane of a deck; reached from the stem lane right-click menu until argument prompts (#9466) can name a stem.';
const NEEDS_SOURCE =
	'Acts on a library source row; reached from the library source right-click menu.';
const NEEDS_DECK_MENU =
	'Opens track info for the deck loaded track; a deck right-click menu item (#9448); reached from that menu until argument prompts (#9466) can name a deck; distinct from browser track.info_open.';
const NEEDS_OTHER_DECK =
	'Names a source or target deck pair; reached from the deck QuickDraw menu until argument prompts (#9466) can pick both decks.';
const NEEDS_DECK_TOGGLE =
	'Toggles a deck transport setting; reached from the deck right-click menu and not wired in the palette bar yet.';
const NEEDS_HISTORY =
	'Acts on one History row or History day; reached from the History view right-click menu (behind its confirm) until argument prompts (#9466) can name a play or a day.';

function chordOf(id: string | null): string | null {
	if (id === null) return null;
	const hit = HOTKEY_REGISTRY.find((entry) => entry.id === id);
	if (hit === undefined) throw new Error(`palette: unknown hotkey ${id}`);
	return hit.chord;
}

function targetDeck(ctx: PaletteContext): PaletteDeck | undefined {
	return ctx.decks.find((d) => d.id === ctx.target);
}

const targetLoaded = (ctx: PaletteContext): boolean => targetDeck(ctx)?.loaded === true;

const targetNotMaster = (ctx: PaletteContext): boolean => {
	const deck = targetDeck(ctx);
	return deck?.loaded === true && deck.is_master !== true;
};

const SPECS: readonly PaletteCommandSpec[] = [
	{
		id: 'deck.unload',
		title: 'Eject deck',
		keywords: ['unload', 'clear', 'empty'],
		args: [{ kind: 'deck' }],
		menu: 'deck.unload',
		hotkey: null,
		run: { kind: 'builtin', row: 'eject' },
		available: targetLoaded
	},
	{
		id: 'deck.play',
		title: 'Play/pause',
		keywords: ['start', 'stop', 'transport', 'space'],
		args: [{ kind: 'deck' }],
		menu: 'deck.play',
		hotkey: 'performance-space',
		run: {
			kind: 'bar',
			build: (_ctx, deck) => ({
				via: 'ipc',
				command: { type: 'play', deck: deck!.id, playing: !deck!.playing }
			})
		},
		available: targetLoaded
	},
	{
		id: 'deck.loop.exit',
		title: 'Exit loop',
		keywords: ['loop', 'clear', 'break'],
		args: [{ kind: 'deck' }],
		menu: 'deck.loop.exit',
		hotkey: 'performance-loop-exit',
		run: {
			kind: 'bar',
			build: (_ctx, deck) => ({ via: 'ipc', command: { type: 'loop', deck: deck!.id, loop: null } })
		},
		available: targetLoaded
	},
	{
		id: 'deck.beat_sync',
		title: 'Beat sync on/off',
		keywords: ['sync', 'tempo', 'match'],
		args: [{ kind: 'deck' }],
		menu: 'deck.beat_sync',
		hotkey: null,
		run: {
			kind: 'bar',
			build: (_ctx, deck) => ({
				via: 'ipc',
				command: { type: 'beat_sync', deck: deck!.id, enabled: !deck!.beat_sync_enabled }
			})
		},
		available: targetLoaded
	},
	{
		id: 'deck.master',
		title: 'Make master',
		keywords: ['master', 'leader', 'sync'],
		args: [{ kind: 'deck' }],
		menu: 'deck.master',
		hotkey: null,
		run: {
			kind: 'bar',
			build: (_ctx, deck) => ({ via: 'ipc', command: { type: 'master', deck: deck!.id } })
		},
		available: targetNotMaster
	},
	{
		id: 'hot_cue.trigger',
		title: 'Trigger hot cue',
		keywords: ['cue', 'jump', 'pad'],
		args: [{ kind: 'deck' }, { kind: 'cue_slot' }],
		menu: 'hot_cue.trigger',
		hotkey: null,
		run: null,
		waits: 'Needs a hot cue slot prompt (#9466).',
		available: targetLoaded
	},
	{
		id: 'hot_cue.save',
		title: 'Save hot cue',
		keywords: ['cue', 'store', 'mark'],
		args: [{ kind: 'deck' }, { kind: 'cue_slot' }],
		menu: 'hot_cue.save',
		hotkey: null,
		run: null,
		waits: 'Needs a hot cue slot and position prompt (#9466).',
		available: targetLoaded
	},
	{
		id: 'hot_cue.clear',
		title: 'Clear hot cue',
		keywords: ['cue', 'delete', 'remove'],
		args: [{ kind: 'deck' }, { kind: 'cue_slot' }],
		menu: 'hot_cue.clear',
		hotkey: null,
		run: null,
		waits: 'Needs a hot cue slot prompt (#9466).',
		available: targetLoaded
	},
	{
		id: 'mixer.eq',
		title: 'Set EQ band',
		keywords: ['eq', 'tone', 'highs'],
		args: [
			{ kind: 'deck' },
			{ kind: 'value', name: 'band', options: ['low', 'mid', 'high'] as const },
			{ kind: 'value', name: 'level', min: 0, max: 1 }
		],
		menu: 'mixer.eq',
		hotkey: null,
		run: null,
		waits: 'Needs EQ band and level prompts (#9466).',
		available: targetLoaded
	},
	{
		id: 'mixer.filter',
		title: 'Set filter',
		keywords: ['filter', 'hpf', 'lpf'],
		args: [{ kind: 'deck' }, { kind: 'value', name: 'filter', min: 0, max: 1 }],
		menu: 'mixer.filter',
		hotkey: null,
		run: null,
		waits: 'Needs a filter value prompt (#9466).',
		available: targetLoaded
	},
	{
		id: 'mixer.channel_cue',
		title: 'Headphone cue on/off',
		keywords: ['cue', 'headphones', 'monitor'],
		args: [{ kind: 'deck' }],
		menu: 'mixer.channel_cue',
		hotkey: null,
		run: null,
		waits: 'The bar does not read the channel headphone cue state yet.',
		available: targetLoaded
	},
	{
		id: 'mixer.assign',
		title: 'Crossfader assign',
		keywords: ['crossfader', 'assign', 'side'],
		args: [
			{ kind: 'deck' },
			{ kind: 'value', name: 'assign', options: ['A', 'B', 'THRU'] as const }
		],
		menu: 'mixer.assign',
		hotkey: null,
		run: null,
		waits: 'Needs a crossfader assign value prompt (#9466).',
		available: targetLoaded
	},
	{
		id: 'settings.open',
		title: 'Open settings',
		keywords: ['preferences', 'config', 'options'],
		args: [],
		menu: null,
		hotkey: 'settings-open',
		run: { kind: 'builtin', row: 'open_settings' },
		available: () => true
	},
	{
		id: 'hotkeys.show',
		title: 'Show hotkeys',
		keywords: ['help', 'shortcuts', 'keys'],
		args: [],
		menu: null,
		hotkey: 'hotkeys-toggle',
		run: { kind: 'bar', build: () => ({ via: 'ui', action: 'show_hotkeys' }) },
		available: () => true
	},
	{
		id: 'hotkeys.peek',
		title: 'Peek at hotkeys',
		keywords: ['hold', 'overlay', 'help'],
		args: [],
		menu: null,
		hotkey: 'hotkeys-hold',
		run: null,
		waits: 'A hold gesture, not a palette pick.',
		available: () => true
	},
	{
		id: 'hotkeys.close',
		title: 'Hide hotkeys',
		keywords: ['dismiss', 'escape', 'close'],
		args: [],
		menu: null,
		hotkey: 'hotkeys-close',
		run: null,
		waits: 'Closes the overlay from inside it, not from the bar.',
		available: () => true
	},
	{
		id: 'assistant.toggle',
		title: 'Assistant sidebar',
		keywords: ['ai', 'sidebar', 'help'],
		args: [],
		menu: null,
		hotkey: 'assistant-toggle',
		run: null,
		waits: 'The assistant sidebar open state lives on the page, not the bar.',
		available: () => true
	},
	{
		id: 'set_notes.toggle',
		title: 'Set notes pad',
		keywords: ['notes', 'pad', 'setlist'],
		args: [],
		menu: null,
		hotkey: 'set-notes-toggle',
		run: null,
		waits: 'The Set notes pad open state lives on the page, not the bar.',
		available: () => true
	},
	{
		id: 'layout.less',
		title: 'Two-deck layout',
		keywords: ['layout', 'decks', 'less'],
		args: [],
		menu: null,
		hotkey: 'deck-layout-less',
		run: null,
		waits: 'The layout preference lives in prefs; the bar does not import prefs.',
		available: () => true
	},
	{
		id: 'layout.more',
		title: 'Four-deck layout',
		keywords: ['layout', 'decks', 'more'],
		args: [],
		menu: null,
		hotkey: 'deck-layout-more',
		run: null,
		waits: 'The layout preference lives in prefs; the bar does not import prefs.',
		available: () => true
	},
	{
		id: 'loop.double',
		title: 'Double loop',
		keywords: ['loop', 'length', 'extend'],
		args: [],
		menu: null,
		hotkey: 'performance-loop-double',
		run: null,
		waits: 'Acts on the last loop the performance hotkeys track, not a bar pick.',
		available: () => true
	},
	{
		id: 'loop.halve',
		title: 'Halve loop',
		keywords: ['loop', 'length', 'shrink'],
		args: [],
		menu: null,
		hotkey: 'performance-loop-halve',
		run: null,
		waits: 'Acts on the last loop the performance hotkeys track, not a bar pick.',
		available: () => true
	},
	{
		id: 'waveform.zoom_out',
		title: 'Zoom waveform out',
		keywords: ['waveform', 'zoom', 'bars'],
		args: [],
		menu: null,
		hotkey: 'performance-wave-zoom-out',
		run: {
			kind: 'bar',
			build: () => ({ via: 'ipc', command: { type: 'wave_zoom_step', direction: 'out' } })
		},
		available: () => true
	},
	{
		id: 'waveform.zoom_in',
		title: 'Zoom waveform in',
		keywords: ['waveform', 'zoom', 'detail'],
		args: [],
		menu: null,
		hotkey: 'performance-wave-zoom-in',
		run: {
			kind: 'bar',
			build: () => ({ via: 'ipc', command: { type: 'wave_zoom_step', direction: 'in' } })
		},
		available: () => true
	},
	{
		id: 'feedback.pin',
		title: 'Drop comment pin',
		keywords: ['comment', 'pin', 'feedback'],
		args: [],
		menu: null,
		hotkey: 'performance-comment-pin',
		run: null,
		waits: 'The pin is placed by the topbar handler, not a bar pick.',
		available: () => true
	},
	{
		id: 'playlist.undo',
		title: 'Undo playlist change',
		keywords: ['revert', 'history', 'undo'],
		args: [],
		menu: null,
		hotkey: 'playlist-undo',
		run: { kind: 'bar', build: () => ({ via: 'ipc', command: { type: 'playlist_undo' } }) },
		available: () => true
	},
	{
		id: 'playlist.redo',
		title: 'Redo playlist change',
		keywords: ['history', 'redo', 'again'],
		args: [],
		menu: null,
		hotkey: 'playlist-redo',
		run: { kind: 'bar', build: () => ({ via: 'ipc', command: { type: 'playlist_redo' } }) },
		available: () => true
	},
	{
		id: 'tech_mode.toggle',
		title: 'Technically-working mode',
		keywords: ['tech', 'debug', 'overlay'],
		args: [],
		menu: null,
		hotkey: 'tech-mode-r',
		run: { kind: 'bar', build: () => ({ via: 'ipc', command: { type: 'tech_mode_toggle' } }) },
		available: () => true
	},
	{
		id: 'tech_mode.reveal',
		title: 'Reveal tech-mode controls',
		keywords: ['hold', 'peek', 'mixer'],
		args: [],
		menu: null,
		hotkey: 'tech-mode-opt',
		run: null,
		waits: 'A hold-only gesture, not a palette pick.',
		available: () => true
	},
	{
		id: 'tech_mode.eq',
		title: 'Floating EQ panel',
		keywords: ['eq', 'panel', 'floating'],
		args: [],
		menu: null,
		hotkey: 'tech-mode-eq',
		run: null,
		waits: 'The bar does not read whether the floating EQ panel is raised.',
		available: () => true
	}
];

const commands: readonly PaletteCommand[] = SPECS.map((spec) => ({
	...spec,
	shortcut: chordOf(spec.hotkey),
	route: spec.menu === null ? null : describeCommand(spec.menu)
}));

const exclusions = {
	'deck.load': 'The bar own track rows load a track onto a deck.',
	'deck.move': NEEDS_OTHER_DECK,
	'deck.instant_double': NEEDS_OTHER_DECK,
	'deck.key_lock': NEEDS_DECK_TOGGLE,
	'deck.quantize': NEEDS_DECK_TOGGLE,
	'deck.slip': NEEDS_DECK_TOGGLE,
	'deck.track_info': NEEDS_DECK_MENU,
	'playlist.select': 'The bar own playlist rows open a playlist (CMDK-6848).',
	'deck.transition_out_here':
		'Names a waveform position; picked where the waveform was right-clicked.',
	'mixer.animate': 'Needs a dial and a ramp expression that only the dial menu names.',
	'mixer.trim': NEEDS_VALUE,
	'mixer.fader': NEEDS_VALUE,
	'mixer.crossfader': NEEDS_VALUE,
	'mixer.auto_gain': NEEDS_VALUE,
	'stem.mute': NEEDS_STEM,
	'stem.solo': NEEDS_STEM,
	'stem.gain': NEEDS_STEM,
	'feedback.comment': 'Needs a feedback pin on the vocal lane.',
	'work.link_candidates': 'A lookup the work-link dialog runs, not a command a DJ picks.',
	'playlist_tree.order': 'A drag in the playlist tree, not a command.',
	'track.add_to_playlist': NEEDS_TRACK,
	'track.edit': NEEDS_TRACK,
	'track.comment': NEEDS_TRACK,
	'track.relocate': NEEDS_TRACK,
	'track.reveal': NEEDS_TRACK,
	'track.playlists': NEEDS_TRACK,
	'track.read': NEEDS_TRACK,
	'track.transition_in.set': NEEDS_TRACK,
	'track.reanalyze': NEEDS_TRACK,
	'track.grid_edit':
		'Locks or edits the grid of the track a deck holds; reached from the deck right-click menu, the deck menu, and the grid buttons; the menu keeps this disabled until a lock flag ships.',
	'track.library_job': NEEDS_TRACK,
	'track.stems.generate': NEEDS_TRACK,
	'track.remove_from_library': NEEDS_TRACK,
	'track.offload': NEEDS_TRACK,
	'track.offload_restore': NEEDS_TRACK,
	'track.pair': NEEDS_TRACK,
	'track.info_open': NEEDS_TRACK,
	'track.related': NEEDS_TRACK,
	'work.link_many': NEEDS_TRACK,
	'work.edit': NEEDS_TRACK,
	'history.delete_play': NEEDS_HISTORY,
	'history.delete_day': NEEDS_HISTORY,
	'playlist.create': NEEDS_LIST,
	'playlist.update': NEEDS_LIST,
	'playlist.delete': NEEDS_LIST,
	'playlist.duplicate': NEEDS_LIST,
	'playlist.remove_items': NEEDS_LIST,
	'playlist.export_txt': NEEDS_LIST,
	'playlist.offload': NEEDS_LIST,
	'playlist.offload_restore': NEEDS_LIST,
	'set.skip': NEEDS_LIST,
	'section.add': NEEDS_LIST,
	'section.update': NEEDS_LIST,
	'section.delete': NEEDS_LIST,
	'smartlist.create': NEEDS_LIST,
	'smartlist.update': NEEDS_LIST,
	'smartlist.delete': NEEDS_LIST,
	'smartlist.duplicate': NEEDS_LIST,
	'library.refresh': NEEDS_SOURCE,
	'library.folders': NEEDS_SOURCE,
	'perf.view':
		'Picks a performance layout view; reached from the empty performance area right-click menu.',
	'app.settings':
		'The settings.open palette entry already opens settings; this id is the menu read of the settings route.',
	'usb.export.plan': NEEDS_VOLUME,
	'usb.eject': NEEDS_VOLUME,
	'usb.volumes': NEEDS_VOLUME
} satisfies Partial<Record<MenuCommandId, string>>;

export const PALETTE_REGISTRY: {
	commands: readonly PaletteCommand[];
	exclusions: Readonly<Partial<Record<MenuCommandId, string>>>;
} = { commands, exclusions };
