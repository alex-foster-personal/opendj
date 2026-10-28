/**
 * The right-click menu registry (W28, ADR-NEW-context-menu-registry, CTXMENU-01..06).
 *
 * One declarative table says, for every right-clickable target kind, which
 * items exist and when each is visible, enabled, or disabled with a reason.
 * One pure resolver, `menuFor(target, ctx)`, reads it. Hosts (TrackContextMenu,
 * TreeContextMenu, QuickDrawMenu) only gather the context and bind each
 * resolved item's command id to a runner; they never decide what shows.
 *
 * Modeled on VS Code's menu contributions: an item is a contribution with a
 * `when` clause (here a typed predicate over the target and context, not a
 * string expression) plus a command id. Unlike VS Code, a contribution may
 * also say WHY it is disabled, because the house rule is that an inert
 * control explains itself.
 *
 * Requirements (mini-PRD):
 *   ✔︎ Every target kind in MenuTargetKind has an entry in MENU_CONTRIBUTIONS
 *     (the mapped type makes a missing kind a type error).
 *     [if] a new kind is added to the union with no contributions [then] ⛔️ svelte-check
 *   ✔︎ Every item carries a command id from MENU_COMMANDS, and every command
 *     id resolves to a real IPC type or HTTP route (unit harness).
 *     [if] an enabled item names a route that is not in openapi.json [then] ⛔️
 *   ✔︎ A disabled item always carries a non-empty reason.
 *     [if] an item resolves disabled with no reason [then] ⛔️
 *   ✔︎ Every menu change shows as a diff in (private path)/<kind>.md.
 *     [if] the registry changes and the golden matrix does not [then] ⛔️
 *
 * Pure: no Svelte, no DOM, no engine import.
 */
import { describeCommand, type MenuCommandId } from './commands';

export type DeckNumber = 1 | 2 | 3 | 4;
export type CueSlot = 'A' | 'B' | 'C' | 'D' | 'E' | 'F' | 'G' | 'H';

/** Where a library row came from. Anything but collection/playlist is read only. */
export type TrackSource = 'collection' | 'playlist' | 'usb' | 'readonly-import';
/** Local audio as the listing reports it (FileAvailabilityStatus, bucketed). */
export type TrackAudio = 'present' | 'missing' | 'streaming';

/** The context each target kind is resolved against. Every field is a finite
 * domain, so the permutation harness can enumerate it. */
export interface MenuContextByKind {
	/** A library row (TrackTable). `selection` counts rows the action covers. */
	track: {
		selection: 'one' | 'many';
		source: TrackSource;
		audio: TrackAudio;
		stems: 'none' | 'ready';
		/** LIBWS-03: Library mode has no decks and starts no workers. */
		workspace: 'performance' | 'library';
	};
	/** A row that only names a track (Suggest-next, Cmd-K, library jobs). */
	'track-chip': { audio: TrackAudio };
	/** The "Playlists" folder header at the top of the tree. */
	'playlists-root': Record<string, never>;
	playlist: { forbidDuplicates: boolean };
	'playlist-folder': Record<string, never>;
	smartlist: Record<string, never>;
	/** Taglists, autolists, Missing Tracks, Recently Deleted, stick trees.
	 * `stick` is true only inside a plugged-in USB stick's tree (USBEXP-04). */
	'tree-readonly': { stick: boolean };
	/** Library source tabs and the icon rail. */
	'library-source': Record<string, never>;
	deck: { loaded: boolean; playing: boolean; looping: boolean; synced: boolean; master: boolean };
	/** moveLoses: what a move to the playhead would drop from the filled cue
	 * (the save command keeps the comment, not a loop or a color). */
	'hot-cue': { loaded: boolean; filled: boolean; moveLoses: 'nothing' | 'color' | 'loop' };
	waveform: { loaded: boolean; looping: boolean; vocalHit: boolean };
	'mixer-channel': {
		eqFlat: boolean;
		filterFlat: boolean;
		cue: boolean;
		assign: 'A' | 'B' | 'THRU';
	};
	/** Right-click on nothing in particular inside the performance page. */
	'perf-empty': Record<string, never>;
}

export type MenuTargetKind = keyof MenuContextByKind;

/** What was right-clicked. Only the fields labels need; ids stay in the host. */
export type MenuTarget =
	| { kind: 'track'; count: number }
	| { kind: 'track-chip' }
	| { kind: 'playlists-root' }
	| { kind: 'playlist' }
	| { kind: 'playlist-folder' }
	| { kind: 'smartlist' }
	| { kind: 'tree-readonly' }
	| { kind: 'library-source' }
	| { kind: 'deck'; deck: DeckNumber }
	| { kind: 'hot-cue'; deck: DeckNumber; slot: CueSlot }
	| { kind: 'waveform'; deck: DeckNumber }
	| { kind: 'mixer-channel'; deck: DeckNumber }
	| { kind: 'perf-empty' };

type TargetOf<K extends MenuTargetKind> = Extract<MenuTarget, { kind: K }>;

/** One contribution. Predicates default to "always". */
export interface MenuContribution<K extends MenuTargetKind> {
	id: string;
	label: string | ((target: TargetOf<K>, ctx: MenuContextByKind[K]) => string);
	command: MenuCommandId;
	/** VS Code's `when`: false hides the item. */
	when?: (ctx: MenuContextByKind[K], target: TargetOf<K>) => boolean;
	/** Non-null means shown but disabled, with this reason as its tooltip. */
	disabledWhen?: (ctx: MenuContextByKind[K], target: TargetOf<K>) => string | null;
	/** Tooltip for an enabled item; defaults to the command it sends. */
	title?: (target: TargetOf<K>) => string;
	/** Present for check items; the value is the checked state. */
	checked?: (ctx: MenuContextByKind[K]) => boolean;
	testId?: (target: TargetOf<K>) => string;
}

export interface ResolvedMenuItem {
	id: string;
	label: string;
	command: MenuCommandId;
	enabled: boolean;
	/** Tooltip: the disabled reason, or the command an agent would send. */
	title: string;
	disabledReason?: string;
	checked?: boolean;
	testId?: string;
}

const DECKS: readonly DeckNumber[] = [1, 2, 3, 4];

const READ_ONLY_SOURCES: ReadonlySet<TrackSource> = new Set(['usb', 'readonly-import']);

function writable(ctx: { source: TrackSource }): boolean {
	return !READ_ONLY_SOURCES.has(ctx.source);
}

/** Starts an analysis, stem or lyrics worker: writable rows outside Library mode. */
function startsWorker(ctx: MenuContextByKind['track']): boolean {
	return writable(ctx) && ctx.workspace === 'performance';
}

const NO_LOCAL_AUDIO: Record<TrackAudio, string | null> = {
	present: null,
	missing: 'the audio file is missing - use Relocate first',
	streaming: 'streaming track - there is no local audio file'
};

function oneTrackOnly(ctx: { selection: 'one' | 'many' }): string | null {
	return ctx.selection === 'many' ? 'works on one track at a time - select a single row' : null;
}

function plural(count: number): string {
	return count === 1 ? '1 track' : `${count} tracks`;
}

const TRACK_MENU: MenuContribution<'track'>[] = [
	...DECKS.map(
		(deck): MenuContribution<'track'> => ({
			id: `load-${deck}`,
			label: `Load to deck ${deck}`,
			command: 'deck.load',
			when: (ctx) => ctx.workspace === 'performance',
			disabledWhen: (ctx) => (ctx.audio === 'missing' ? NO_LOCAL_AUDIO.missing : null),
			title: () => `opendj load ${deck} {stable_id}`,
			testId: () => `context-menu-load-deck-${deck}`
		})
	),
	{
		id: 'add-playlist',
		label: 'Add to playlist...',
		command: 'track.add_to_playlist',
		when: writable
	},
	{
		id: 'bulk-edit',
		label: (target) => `Bulk edit (${target.count})`,
		command: 'track.edit',
		when: writable
	},
	{ id: 'find-replace', label: 'Find/replace', command: 'track.edit', when: writable },
	{ id: 'mytag', label: 'My Tag editor', command: 'track.edit', when: writable },
	{
		id: 'relocate',
		label: 'Relocate',
		command: 'track.relocate',
		when: (ctx) => writable(ctx) && ctx.audio !== 'streaming',
		disabledWhen: oneTrackOnly
	},
	{
		id: 'finder',
		label: 'Show in Finder',
		command: 'track.reveal',
		when: writable,
		disabledWhen: (ctx) => NO_LOCAL_AUDIO[ctx.audio]
	},
	{
		id: 'show-in-playlists',
		label: 'Show in playlists',
		command: 'track.playlists',
		when: writable,
		disabledWhen: oneTrackOnly
	},
	{
		id: 'copy-path',
		label: 'Copy path',
		command: 'track.read',
		when: writable,
		disabledWhen: (ctx) => (ctx.audio === 'streaming' ? NO_LOCAL_AUDIO.streaming : null)
	},
	{
		id: 'analyze',
		label: (target) => (target.count > 1 ? `Re-analyze ${plural(target.count)}` : 'Re-analyze'),
		command: 'track.reanalyze',
		when: startsWorker,
		disabledWhen: (ctx) => NO_LOCAL_AUDIO[ctx.audio]
	},
	{
		id: 'stems-generate',
		label: (_target, ctx) =>
			ctx.stems === 'ready' && ctx.selection === 'one' ? 'Stems: regenerate next' : 'Stems: do next',
		command: 'track.library_job',
		when: startsWorker,
		disabledWhen: (ctx) => NO_LOCAL_AUDIO[ctx.audio]
	},
	{
		id: 'lyrics',
		label: 'Lyrics: do next',
		command: 'track.library_job',
		when: startsWorker,
		disabledWhen: (ctx) => NO_LOCAL_AUDIO[ctx.audio]
	},
	{
		id: 'remove-playlist',
		label: 'Remove from playlist',
		command: 'playlist.remove_items',
		when: (ctx) => ctx.source === 'playlist'
	},
	{
		id: 'remove-library',
		label: (target) =>
			target.count > 1 ? `Remove ${plural(target.count)} from library` : 'Remove from library',
		command: 'track.remove_from_library',
		when: writable
	}
];

/** The two "New ..." items apply to every tree target; they ignore the context. */
function newItems<K extends 'playlists-root' | 'playlist' | 'playlist-folder' | 'smartlist'>(): MenuContribution<K>[] {
	return [
		{ id: 'new-playlist', label: 'New playlist', command: 'playlist.create' },
		{ id: 'new-smartlist', label: 'New smartlist', command: 'smartlist.create' }
	];
}

const DECK_UNLOAD: MenuContribution<'deck' | 'waveform'> = {
	id: 'unload',
	label: (target) => `Unload CH${target.deck}`,
	command: 'deck.unload',
	disabledWhen: (ctx, target) => (ctx.loaded ? null : `CH${target.deck} is empty`)
};
const DECK_EXIT_LOOP: MenuContribution<'deck' | 'waveform'> = {
	id: 'exit-loop',
	label: (target) => `Exit Loop CH${target.deck}`,
	command: 'deck.loop.exit',
	when: (ctx) => ctx.loaded && ctx.looping
};

export const MENU_CONTRIBUTIONS: { [K in MenuTargetKind]: MenuContribution<K>[] } = {
	track: TRACK_MENU,
	'track-chip': [
		...DECKS.map(
			(deck): MenuContribution<'track-chip'> => ({
				id: `load-${deck}`,
				label: `Load to CH${deck}`,
				command: 'deck.load',
				disabledWhen: (ctx) => (ctx.audio === 'missing' ? NO_LOCAL_AUDIO.missing : null),
				testId: () => `quick-draw-load-ch${deck}`
			})
		),
		{
			// Expanded by the host into one item per stem tier (INSTALL-32).
			id: 'stems-tiers',
			label: 'Stems',
			command: 'track.stems.generate',
			when: (ctx) => ctx.audio === 'present'
		}
	],
	'playlists-root': [
		...newItems<'playlists-root'>(),
		// LIBTREE-02: writes the whole tree order, the same route a drag uses.
		{ id: 'sort-name', label: 'Sort playlists A-Z', command: 'playlist_tree.order' }
	],
	playlist: [
		...newItems<'playlist'>(),
		{ id: 'rename', label: 'Rename', command: 'playlist.update' },
		{ id: 'delete', label: 'Delete', command: 'playlist.delete' },
		{ id: 'duplicate', label: 'Duplicate', command: 'playlist.duplicate' },
		{
			id: 'forbid-duplicates',
			label: 'Forbid duplicates',
			command: 'playlist.update',
			checked: (ctx) => ctx.forbidDuplicates
		},
		{ id: 'reveal', label: 'Reveal in tree', command: 'playlist.select' },
		{
			// USBEXP-04: opens the USB export screen with this playlist's name.
			id: 'export-usb',
			label: 'Export to USB...',
			command: 'usb.export.plan',
			title: () => 'Open the USB export screen for this playlist (POST /api/v1/usb-export/plan)'
		}
	],
	'playlist-folder': [
		...newItems<'playlist-folder'>(),
		{ id: 'rename', label: 'Rename', command: 'playlist.update' },
		{ id: 'delete', label: 'Delete', command: 'playlist.delete' }
	],
	smartlist: [
		...newItems<'smartlist'>(),
		{ id: 'rename', label: 'Rename', command: 'smartlist.update' },
		{ id: 'delete', label: 'Delete', command: 'smartlist.delete' },
		{ id: 'duplicate', label: 'Duplicate', command: 'smartlist.duplicate' }
	],
	// Empty off a stick: those surfaces have no write API behind them, and an
	// empty menu means "suppress the wrong menu", not "show the browser's".
	// A USB stick's tree gets the device rows (USBEXP-04).
	'tree-readonly': [
		{
			id: 'usb-status',
			label: 'USB status...',
			command: 'usb.volumes',
			when: (ctx) => ctx.stick,
			title: () => 'Open the USB export screen: sticks, access and export (GET /api/v1/usb/volumes)'
		},
		{
			id: 'usb-sync',
			label: 'Sync to stick',
			command: 'usb.export.plan',
			when: (ctx) => ctx.stick,
			disabledWhen: () =>
				'no playlist sync to a stick yet: use Export to USB on a playlist, which writes exportLibrary.db only'
		},
		{
			id: 'usb-eject',
			label: 'Eject stick',
			command: 'usb.eject',
			when: (ctx) => ctx.stick
		}
	],
	'library-source': [],
	deck: [
		{
			id: 'play',
			label: (target, ctx) => `${ctx.playing ? 'Pause' : 'Play'} CH${target.deck}`,
			command: 'deck.play',
			disabledWhen: (ctx, target) => (ctx.loaded ? null : `CH${target.deck} is empty`)
		},
		DECK_UNLOAD as MenuContribution<'deck'>,
		DECK_EXIT_LOOP as MenuContribution<'deck'>,
		{
			id: 'beat-sync',
			label: 'Beat sync',
			command: 'deck.beat_sync',
			checked: (ctx) => ctx.synced,
			disabledWhen: (ctx, target) => (ctx.loaded ? null : `CH${target.deck} is empty`)
		},
		{
			id: 'make-master',
			label: 'Make master',
			command: 'deck.master',
			when: (ctx) => !ctx.master,
			disabledWhen: (ctx, target) => (ctx.loaded ? null : `CH${target.deck} is empty`)
		}
	],
	'hot-cue': [
		{
			id: 'cue-jump',
			label: (target) => `Jump to cue ${target.slot}`,
			command: 'hot_cue.trigger',
			when: (ctx) => ctx.filled
		},
		{
			id: 'cue-save',
			label: (target, ctx) =>
				ctx.filled ? `Move cue ${target.slot} to playhead` : `Set cue ${target.slot} at playhead`,
			command: 'hot_cue.save',
			disabledWhen: (ctx, target) => {
				if (!ctx.loaded) return `CH${target.deck} is empty`;
				if (ctx.moveLoses === 'loop') return `cue ${target.slot} is a loop - moving it would turn it into a point cue`;
				if (ctx.moveLoses === 'color') return `moving cue ${target.slot} would drop its color`;
				return null;
			}
		},
		{
			id: 'cue-clear',
			label: (target) => `Delete cue ${target.slot}`,
			command: 'hot_cue.clear',
			when: (ctx) => ctx.filled
		}
	],
	waveform: [
		{
			// The vocal-lane Fix / add comment pin (FB-12); its own probe may
			// still disable it at run time when the daemon serves no feedback API.
			id: 'vocal-fix-comment',
			label: 'Fix / add comment',
			command: 'feedback.comment',
			when: (ctx) => ctx.loaded && ctx.vocalHit,
			testId: () => 'quick-draw-vocal-fix'
		},
		DECK_UNLOAD as MenuContribution<'waveform'>,
		DECK_EXIT_LOOP as MenuContribution<'waveform'>
	],
	'mixer-channel': [
		{
			id: 'eq-reset',
			label: 'Reset EQ',
			command: 'mixer.eq',
			disabledWhen: (ctx) => (ctx.eqFlat ? 'EQ is already flat' : null)
		},
		{
			id: 'filter-reset',
			label: 'Reset filter',
			command: 'mixer.filter',
			disabledWhen: (ctx) => (ctx.filterFlat ? 'filter is already off' : null)
		},
		{
			id: 'channel-cue',
			label: 'Headphone cue',
			command: 'mixer.channel_cue',
			checked: (ctx) => ctx.cue
		},
		...(['A', 'B', 'THRU'] as const).map(
			(assign): MenuContribution<'mixer-channel'> => ({
				id: `assign-${assign.toLowerCase()}`,
				label: assign === 'THRU' ? 'Crossfader: thru' : `Crossfader: ${assign}`,
				command: 'mixer.assign',
				checked: (ctx) => ctx.assign === assign
			})
		)
	],
	'perf-empty': []
};

/**
 * Resolve the menu for one right-click. Pure and total: the same target and
 * context always give the same items, in contribution order.
 */
export function menuFor<K extends MenuTargetKind>(
	target: TargetOf<K>,
	ctx: MenuContextByKind[K]
): ResolvedMenuItem[] {
	const contributions = MENU_CONTRIBUTIONS[target.kind as K] as MenuContribution<K>[];
	const items: ResolvedMenuItem[] = [];
	for (const c of contributions) {
		if (c.when !== undefined && !c.when(ctx, target)) continue;
		const reason = c.disabledWhen?.(ctx, target) ?? null;
		const item: ResolvedMenuItem = {
			id: c.id,
			label: typeof c.label === 'string' ? c.label : c.label(target, ctx),
			command: c.command,
			enabled: reason === null,
			title: reason ?? c.title?.(target) ?? describeCommand(c.command)
		};
		if (reason !== null) item.disabledReason = reason;
		if (c.checked !== undefined) item.checked = c.checked(ctx);
		if (c.testId !== undefined) item.testId = c.testId(target);
		items.push(item);
	}
	return items;
}

/** Bucket a listing's FileAvailabilityStatus for the registry. */
export function trackAudioOf(row: {
	file_availability?: string | null;
	file_exists?: boolean | null;
	is_streaming?: boolean | null;
}): TrackAudio {
	const availability = row.file_availability ?? '';
	if (row.is_streaming === true || availability === 'streaming' || availability.endsWith('-streaming')) {
		return 'streaming';
	}
	if (availability === 'absent' || row.file_exists === false) return 'missing';
	return 'present';
}
