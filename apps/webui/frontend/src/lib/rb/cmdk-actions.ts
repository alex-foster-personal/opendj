/**
 * CMDK-11..14: settings and commands as Cmd K results, runeless so the unit
 * suite loads them.
 *
 * Settings come from the Settings overlay's own catalog and search
 * (`filterSettings`), so a row carries the catalog's label and reads and
 * writes the same store the Settings overlay does: nothing here copies a
 * label or keeps a second value. Only rows Enter can act on are offered: a
 * boolean flips, an enum moves to the option the query names (else the next
 * one), and a link opens its page. Number sliders, path lists and the
 * unbuilt todo rows stay in the Settings overlay.
 *
 * Commands are built from live deck state: one "Eject deck N" per loaded
 * deck, plus runnable rows from the palette registry (CMDK-9600). A playing
 * deck asks first (the play-interrupt rule), like the deck picker's replace
 * guard. In the Tauri shell a "Library on second screen" row opens or focuses
 * the second-screen library window (SECONDSCREEN-11715).
 */
import { PALETTE_REGISTRY, type PaletteBarRun, type PaletteContext, type PaletteDeck } from '$lib/palette/commands';
import type { SettingDef } from '$lib/settings/catalog-types';
import { settingsQueryTokens } from '$lib/settings/search';
import type { PerformanceCommandId } from '$lib/keyboard/command-registry';
import type { DeckId } from './deck-id';

/** Rows the "Commands and settings" group draws at most. */
export const ACTION_LIMIT = 6;

/** Tracks join the results from this many typed characters (spaces do not count). */
export const TRACK_MIN_CHARS = 2;

export type SettingValue = boolean | string;

export interface SettingAction {
	kind: 'setting';
	id: string;
	label: string;
	/** "Settings > Performance": where the row lives in the Settings overlay. */
	path: string;
	def: SettingDef;
	/** The current value, as `readSettingValue` reported it. */
	value: SettingValue;
	/**
	 * The value Enter writes, as the query the row was built from decides it (an enum's target is
	 * named by the query's tokens). Part of the row's id, so a query that moves the target refuses a stale pick.
	 */
	next?: SettingValue | null;
}

export interface CommandAction {
	kind: 'command';
	id: string;
	label: string;
	hint: string;
	command:
		| { type: 'eject'; deck: DeckId }
		| { type: 'open_settings'; query: string }
		| { type: 'open_second_screen' }
		| { type: 'performance'; id: PerformanceCommandId }
		| { type: 'palette'; run: PaletteBarRun };
	/** Registry keywords for ranking and `actionRows` token matching. */
	keywords?: readonly string[];
}

export type CmdkAction = SettingAction | CommandAction;

/** The deck fields the commands read. */
export interface ActionDeck {
	id: DeckId;
	loaded: boolean;
	playing: boolean;
	title: string | null;
	/** The load the deck holds now: an eject row pins it, so a reload makes the pinned row stale (Codex P2 BLOCKING, #6691). */
	load_generation: number;
}

/** Characters typed as the user sees them: grapheme clusters, so a decomposed accent or a ZWJ emoji is one (CMDK-11). */
export function queryChars(query: string): number {
	return [...new Intl.Segmenter().segment(query.replace(/\s+/g, ''))].length;
}

/** True once the query is long enough for tracks to join the results. */
export function tracksAllowed(query: string): boolean {
	return queryChars(query) >= TRACK_MIN_CHARS;
}

/** A catalog row Enter can act on from the bar. */
export function actionableSetting(def: SettingDef): boolean {
	if (!def.implemented) return false;
	const kind = def.control.kind;
	return kind === 'boolean' || kind === 'enum' || kind === 'link';
}

function lower(text: string): string {
	return text.toLowerCase();
}

/**
 * Query words, split exactly as the settings search splits them, so the row
 * the search offers and the option Enter switches to read the same words:
 * "gothic," names Gothic in both (Sol r4278366200).
 */
export function actionTokens(query: string): string[] {
	return settingsQueryTokens(query);
}

/** Every token is found in the text (a prefix of a word counts). */
function matchesAll(text: string, tokens: readonly string[]): boolean {
	const hay = lower(text);
	return tokens.every((token) => hay.includes(token));
}

/**
 * An enum row's value names exactly one of its catalog options. A value the
 * catalog does not list is a store/catalog mismatch: the bar names it and
 * Enter refuses it, never stepping on to the first option (Sol r4279823899).
 */
export function enumValueKnown(def: SettingDef, value: SettingValue): boolean {
	if (def.control.kind !== 'enum') return false;
	return def.control.options.filter((option) => option.value === value).length === 1;
}

/**
 * The enum value Enter moves to: the option whose label or value a query
 * token names (when it is not already set), else the next option in order.
 * Throws on a non-enum row, which has no options to choose from, and on a
 * current value that is not exactly one of its options.
 */
export function enumTarget(def: SettingDef, current: SettingValue, tokens: readonly string[]): string {
	if (def.control.kind !== 'enum') throw new Error(`setting ${def.id} is not an enum`);
	const options = def.control.options;
	if (options.length === 0) throw new Error(`setting ${def.id} has no options`);
	if (!enumValueKnown(def, current)) throw new Error(`${def.label} holds ${JSON.stringify(current)}, which is not one of its options; fix it in Settings`);
	const named = options.find(
		(option) =>
			option.value !== current &&
			tokens.some((token) => token.length >= 2 && (lower(option.label).includes(token) || lower(option.value).includes(token)))
	);
	if (named !== undefined) return named.value;
	const at = options.findIndex((option) => option.value === current);
	return options[(at + 1) % options.length].value;
}

/** A boolean row holds a real boolean; a stored string or number is a store or catalog fault, not Off (Sol P1, #6689). */
export function booleanValueKnown(def: SettingDef, value: SettingValue): boolean {
	return def.control.kind === 'boolean' && typeof value === 'boolean';
}

/** The label of the value a setting row shows: On or Off, or the enum option's own label. */
export function settingValueLabel(def: SettingDef, value: SettingValue): string {
	if (def.control.kind === 'boolean') {
		if (!booleanValueKnown(def, value)) return `${String(value)} (not a boolean)`;
		return value === true ? 'On' : 'Off';
	}
	if (def.control.kind === 'enum') {
		if (!enumValueKnown(def, value)) return `${String(value)} (not an option)`;
		return def.control.options.find((option) => option.value === value)?.label ?? String(value);
	}
	return '';
}

/** What Enter does on a setting row, in words, for the hint beside its value. */
export function settingVerb(def: SettingDef, value: SettingValue, tokens: readonly string[]): string {
	if (def.control.kind === 'boolean') {
		if (!booleanValueKnown(def, value)) return 'Not a boolean; fix it in Settings';
		return 'Enter to toggle';
	}
	if (def.control.kind === 'enum') {
		if (!enumValueKnown(def, value)) return 'Not one of its options; fix it in Settings';
		const next = enumTarget(def, value, tokens);
		return `Enter for ${settingValueLabel(def, next)}`;
	}
	return 'Enter to open';
}

/** The value Enter writes on a setting row; a link row writes nothing (null). */
export function settingNextValue(def: SettingDef, value: SettingValue, tokens: readonly string[]): SettingValue | null {
	// Like an enum row, a value that is not what the row holds throws rather than overwriting it with a guess (Sol P1, #6689).
	if (def.control.kind === 'boolean') {
		if (!booleanValueKnown(def, value)) throw new Error(`${def.label} holds ${JSON.stringify(value)}, which is not a boolean; fix it in Settings`);
		return value !== true;
	}
	if (def.control.kind === 'enum') return enumTarget(def, value, tokens);
	return null;
}

/** An option of an enum row, other than the current one, that a query token names. */
function namesOtherOption(def: SettingDef, value: SettingValue, tokens: readonly string[]): boolean {
	if (def.control.kind !== 'enum') return false;
	return def.control.options.some(
		(option) =>
			option.value !== value &&
			tokens.some((token) => token.length >= 2 && (lower(option.label).includes(token) || lower(option.value).includes(token)))
	);
}

/**
 * Settings in the order Enter should reach them: a row whose option the query
 * names (and would switch to) first, so "gothic" lands on the skin and not on
 * a row that merely mentions skins; then rows whose label or current value
 * the query names; then the rest. Stable within each tier, so the catalog
 * order holds.
 */
export function rankSettings(settings: readonly SettingAction[], query: string): SettingAction[] {
	const tokens = actionTokens(query);
	const tier = (action: SettingAction): number => {
		if (namesOtherOption(action.def, action.value, tokens)) return 0;
		const shown = `${action.label} ${settingValueLabel(action.def, action.value)}`;
		return tokens.length > 0 && matchesAll(shown, tokens) ? 1 : 2;
	};
	return settings
		.map((action, at) => ({ action, at, tier: tier(action) }))
		.sort((a, b) => a.tier - b.tier || a.at - b.at)
		.map(({ action }) => action);
}

/**
 * One "Eject deck N" per loaded deck, the second-screen row when the shell can open
 * that window, plus "Open settings" carrying the query.
 */
export function commandCandidates(
	decks: readonly ActionDeck[],
	query: string,
	options: { secondScreen?: boolean } = {}
): CommandAction[] {
	const out: CommandAction[] = [];
	for (const deck of decks) {
		if (!deck.loaded) continue;
		out.push({
			kind: 'command',
			id: `eject-${deck.id}:${deck.load_generation}`,
			label: `Eject deck ${deck.id}`,
			hint: `${deck.title ?? '(untitled)'}${deck.playing ? ' · playing, asks first' : ''}`,
			command: { type: 'eject', deck: deck.id }
		});
	}
	if (options.secondScreen === true) {
		out.push({
			kind: 'command',
			id: 'open-second-screen',
			label: 'Library on second screen',
			hint: 'Cmd Shift L',
			keywords: ['window', 'display', 'monitor', 'library', 'second screen'],
			command: { type: 'open_second_screen' }
		});
	}
	const q = query.trim();
	out.push({
		kind: 'command',
		id: 'open-settings',
		label: q === '' ? 'Open settings' : `Open settings for "${q}"`,
		hint: 'Cmd ,',
		command: { type: 'open_settings', query: q }
	});
	return out;
}

function keyHint(defaultKey: string | undefined): string {
	if (defaultKey === undefined || defaultKey === '') return '';
	return defaultKey.startsWith('Mod+') ? `Cmd ${defaultKey.slice('Mod+'.length)}` : defaultKey;
}

/** CMDK rows for the command registry's dial/fader commands: offered only while a dial or fader is selected (#9453). */
export function registryCommandCandidates(
	defs: readonly { id: PerformanceCommandId; label: string; defaultKeys: readonly string[] }[]
): CommandAction[] {
	return defs.map((def) => ({
		kind: 'command',
		id: def.id,
		label: def.label,
		hint: keyHint(def.defaultKeys[0]),
		command: { type: 'performance', id: def.id }
	}));
}

function paletteTargetDeck(ctx: PaletteContext): PaletteDeck | undefined {
	return ctx.decks.find((d) => d.id === ctx.target);
}

/** Runnable palette registry rows for the bar's target deck (CMDK-9600). */
export function paletteCommandRows(ctx: PaletteContext, query: string): CommandAction[] {
	if (actionTokens(query).length === 0) return [];
	const out: CommandAction[] = [];
	for (const entry of PALETTE_REGISTRY.commands) {
		if (entry.run?.kind !== 'bar' || !entry.available(ctx)) continue;
		const needsDeck = entry.args.some((a) => a.kind === 'deck');
		const deck: PaletteDeck | null = needsDeck ? (paletteTargetDeck(ctx) ?? null) : null;
		if (needsDeck && (deck === null || !deck.loaded)) continue;
		const built = entry.run.build(ctx, deck);
		let id = `palette:${entry.id}`;
		if (deck !== null) id += `@${deck.id}:${deck.load_generation}`;
		if (built.via === 'ipc') {
			const cmd = built.command;
			if ('playing' in cmd) id += `=${String(cmd.playing)}`;
			if ('enabled' in cmd) id += `=${String(cmd.enabled)}`;
		}
		const label = deck !== null ? `${entry.title} deck ${deck.id}` : entry.title;
		const hint = entry.shortcut ?? entry.route ?? '';
		out.push({
			kind: 'command',
			id,
			label,
			hint,
			keywords: entry.keywords,
			command: { type: 'palette', run: built }
		});
	}
	return out;
}

/** Frecency key, stable across reloads of a deck (`eject-2:7` → `eject-2`). */
export function commandKey(action: CommandAction): string {
	return action.id.split(':')[0]!;
}

/**
 * The "Commands and settings" group: commands whose label matches every
 * token, then the settings the overlay's own search returned (already in
 * catalog order), capped at `limit`. An empty query offers nothing here.
 * When `ranked` is true, `commands` are already filtered and ordered by rankCommands.
 */
export function actionRows(
	query: string,
	commands: readonly CommandAction[],
	settings: readonly SettingAction[],
	limit = ACTION_LIMIT,
	ranked = false
): CmdkAction[] {
	const tokens = actionTokens(query);
	if (tokens.length === 0) return [];
	const cmds = ranked
		? commands
		: commands.filter((cmd) =>
				matchesAll(`${cmd.label} ${cmd.id.replace(/-/g, ' ')} ${(cmd.keywords ?? []).join(' ')}`, tokens)
			);
	return [...cmds, ...settings].slice(0, limit);
}

/** What picking an eject does: ask first on a playing deck, else run. */
export function ejectAction(deck: { playing: boolean }, confirmed: boolean): 'ask' | 'run' {
	return deck.playing && !confirmed ? 'ask' : 'run';
}

/** The platform's quick-pick modifier: Cmd on macOS, Ctrl elsewhere. */
export function quickPickModifier(platform: string): 'Meta' | 'Control' {
	return /mac|iphone|ipad/i.test(platform) ? 'Meta' : 'Control';
}

/**
 * Whether the quick-pick modifier is still down on the chord that opened the bar: the
 * modifier's own keydown came while the bar was closed, so the numbers show from the
 * opening chord rather than only after the modifier is pressed again.
 */
export function quickModifierHeld(event: { metaKey: boolean; ctrlKey: boolean }, modifier: 'Meta' | 'Control'): boolean {
	return modifier === 'Meta' ? event.metaKey : event.ctrlKey;
}

/**
 * The row a quick-pick key names: 0-8 for the modifier plus 1-9 (the digit
 * row, by code, so a layout's shifted digits still pick), else null. Another
 * modifier held with it (Alt picks the deck, Shift is not a quick pick) is
 * not a quick pick.
 */
export function quickPickIndex(
	event: { code: string; metaKey: boolean; ctrlKey: boolean; altKey: boolean; shiftKey: boolean },
	modifier: 'Meta' | 'Control'
): number | null {
	const held = modifier === 'Meta' ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
	if (!held || event.altKey || event.shiftKey) return null;
	const digit = /^Digit([1-9])$/.exec(event.code);
	return digit === null ? null : Number(digit[1]) - 1;
}

/** What a track row carries when it is one of the master's pairs (CMDK-17). */
export interface CmdkPairTag {
	/** A captured sync snapshot exists, so it loads at its offset with the fader at 0. */
	aligned: boolean;
	/** For an aligned pair: the master and snapshot it was read against (pairBasis), part of its wire id. */
	basis?: string | undefined;
}

/** One selectable row of the bar: a track (maybe one of the master's pairs), or a setting or command. */
export type CmdkEntry<T> = { kind: 'track'; row: T; pair?: CmdkPairTag | undefined } | { kind: 'action'; action: CmdkAction };

export type CmdkGroupId = 'ai' | 'pairs' | 'recent' | 'actions' | 'playlist' | 'collection' | 'settings' | 'commands';

export interface CmdkEntryGroup<T> {
	id: CmdkGroupId;
	/** Matches in the group, when it can hold more than it draws (tracks only). */
	total?: number;
	entries: CmdkEntry<T>[];
}

/** Recent loads the bar lists at most. */
export const RECENT_LIMIT = 5;
/** Settings and commands under the tracks once tracks have joined (prototype: 4 settings). */
export const SETTINGS_UNDER_TRACKS = 4;
/** Commands offered with nothing typed. */
export const COMMANDS_ON_OPEN = 2;

/**
 * The bar's groups in the order drawn (CMDK-11, CMDK-17):
 * - nothing typed: the master deck's pairs (when given), recent loads that are
 *   not among them, a couple of commands, then the open playlist;
 * - one character: commands and settings, then recent loads that match; no
 *   track search runs, and `keepTyping` asks for one more character;
 * - two or more: the playlist and collection hits, then settings, then commands.
 * `recent` is already filtered to the query by the caller.
 */
export function layoutGroups<T>(input: {
	query: string;
	recent: readonly T[];
	playlist: { rows: readonly T[]; total: number };
	collection: { rows: readonly T[]; total: number };
	commands: readonly CommandAction[];
	settings: readonly SettingAction[];
	/** The master deck's pairs, shown only with nothing typed. */
	pairs?: readonly { row: T; aligned: boolean; basis?: string | undefined }[];
	/** A row's stable id, so recent loads already listed as pairs are not listed twice. */
	idOf?: (row: T) => string;
	/** Commands came through rankCommands: matched and ordered for this query; frecency first when nothing is typed. */
	commandsRanked?: boolean;
}): { groups: CmdkEntryGroup<T>[]; keepTyping: boolean } {
	const commandsRanked = input.commandsRanked ?? false;
	const tracks = (rows: readonly T[]): CmdkEntry<T>[] => rows.map((row) => ({ kind: 'track', row }));
	const acts = (rows: readonly CmdkAction[]): CmdkEntry<T>[] => rows.map((action) => ({ kind: 'action', action }));
	const groups: CmdkEntryGroup<T>[] = [];
	const push = (group: CmdkEntryGroup<T>): void => {
		if (group.entries.length > 0) groups.push(group);
	};
	const recent = input.recent.slice(0, RECENT_LIMIT);
	// Nothing typed is judged on the query itself: a query the settings search has no words for
	// (non-ASCII, punctuation) is still typed and searches tracks (Sol r4279823886).
	if (input.query.trim().length === 0) {
		const pairs = input.pairs ?? [];
		push({ id: 'pairs', entries: pairs.map((pair) => ({ kind: 'track', row: pair.row, pair: { aligned: pair.aligned, ...(pair.basis === undefined ? {} : { basis: pair.basis }) } })) });
		const idOf = input.idOf;
		const paired = idOf === undefined ? new Set<string>() : new Set(pairs.map((pair) => idOf(pair.row)));
		push({ id: 'recent', entries: tracks(input.recent.filter((row) => idOf === undefined || !paired.has(idOf(row))).slice(0, RECENT_LIMIT)) });
		push({ id: 'commands', entries: acts(input.commands.slice(0, COMMANDS_ON_OPEN)) });
		push({ id: 'playlist', total: input.playlist.total, entries: tracks(input.playlist.rows) });
		return { groups, keepTyping: false };
	}
	if (!tracksAllowed(input.query)) {
		push({ id: 'actions', entries: acts(actionRows(input.query, input.commands, input.settings, undefined, commandsRanked)) });
		push({ id: 'recent', entries: tracks(recent) });
		return { groups, keepTyping: true };
	}
	push({ id: 'playlist', total: input.playlist.total, entries: tracks(input.playlist.rows) });
	push({ id: 'collection', total: input.collection.total, entries: tracks(input.collection.rows) });
	push({ id: 'settings', entries: acts(input.settings.slice(0, SETTINGS_UNDER_TRACKS)) });
	push({ id: 'commands', entries: acts(actionRows(input.query, input.commands, [], undefined, commandsRanked)) });
	return { groups, keepTyping: false };
}

/** Most recent first, each track once, at most `limit`. */
export function noteRecentLoad(recent: readonly string[], stable_id: string, limit = RECENT_LIMIT): string[] {
	return [stable_id, ...recent.filter((id) => id !== stable_id)].slice(0, limit);
}

/** CMDK-15: the v2 Cmd K bar's open state, serializable for browser agents. */
export interface CmdkSnapshot {
	open: boolean;
	query: string;
	selected: number;
	/** The deck a pick without `deck` loads to: the bar's target, which the deck rings and Alt+1-4 change (Codex P2 BLOCKING, #6691). */
	target: DeckId;
	/** The rows drawn, in order: tracks by stable_id (a master's pair says aligned or loose), settings and commands by id. */
	rows: Array<{
		/** CMDK-6848: a `playlist` row is a library playlist, by its playlist id; picking it opens it as a tree click does. */
		kind: 'track' | 'playlist' | 'setting' | 'command';
		id: string;
		label: string;
		pair?: 'aligned' | 'loose';
		fit?: number;
		/** A setting row: the value it shows (On, Off or the option's label), as the bar draws it (Codex P2 BLOCKING, #6691). */
		value?: string;
		/** A setting row: what Enter does to it for the current query, in the row's own words (Codex P2 BLOCKING, #6691). */
		action?: string;
	}>;
	/** CMDK-20: plain search, or AI search (each track row then carries JEV's `fit`). */
	mode: 'plain' | 'ai';
	/** CMDK-21: the AI answer's state: idle, working, ready, no-key or error. */
	ai: 'idle' | 'working' | 'ready' | 'no-key' | 'error';
	/** With pairs on and a master loaded: the master's pairs lookup (a pair row's id is `pair:<stable_id>:loose` or `pair:<stable_id>:aligned:<basis>`). */
	pairs?: { status: 'loading' | 'ready' } | { status: 'error'; message: string };
	/** While open: the aligned pair loading onto a deck, or the last pick's failure, as the footer shows them. */
	operation?: { status: 'loading'; title: string; deck: DeckId } | { status: 'error'; message: string };
	/** While open: the collection index and search warmup; `loading` or `error` means the track rows are not a search result yet. */
	collection?: { status: 'loading' | 'ready' } | { status: 'error'; message: string };
	/** While open: the deck picker or eject confirmation that holds the bar, so the rows above are not the choice being made (CMDK-15, Codex #6691). */
	dialog?: { kind: 'deck-picker'; deck: DeckId; title: string; confirming?: boolean } | { kind: 'eject'; deck: DeckId };
}

/**
 * The mounted v2 bar registers this with the dispatcher (`registerCmdkAdapter`), the same decoupling as the playlist
 * history panel. performance-ipc declares the same shape (fan-out budget); the /performance binding checks them together.
 */
export interface CmdkAdapter {
	open(query: string | null): Promise<void>;
	close(): void;
	snapshot(): CmdkSnapshot;
	/**
	 * Pick row `index` as Enter does (CMDK-19): a track loads to `deck` (else the
	 * bar's target) without the picker, `confirm` stands for Cmd+Enter over a
	 * playing deck, `vocalsOnly` is Shift+Enter. Rejects with the reason when it does not run.
	 * With `id` (the row id `query().cmdk` reported), a row that is no longer that one is refused.
	 */
	pick(index: number, deck: DeckId | null, confirm: boolean, id: string | null, target: DeckId | null, vocalsOnly: boolean, source: 'user-ui' | 'agent-command'): Promise<void>;
	/** CMDK-20: switch plain and AI search as Tab does; rejects when the bar is closed or the flag is off. */
	setMode(mode: 'plain' | 'ai'): void;
}
