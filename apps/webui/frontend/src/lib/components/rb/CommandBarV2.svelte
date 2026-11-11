<script lang="ts">
	// CMDK-04..06: the redesigned Cmd K bar, behind the `cmdk.v2` build flag
	// (the shipped CommandBar.svelte stays when the flag is off).
	//
	// One search covers the open playlist and the whole collection in one
	// in-memory pass (the pane's rows and the LIBM-171 library index), so both
	// groups arrive together; playlist hits list first and the collection never
	// repeats them. The library's filters sit on a row of icons under the input
	// (same prefs as the library checkboxes), and small deck rings sit above the
	// bar's top-right corner. Typing goes straight in: the input takes focus as
	// the bar opens, and printable keys pressed before it has focus are put into
	// the search instead of being lost. Loading goes through the browser's own
	// deck-load path (the `load` prop), so every guard that applies to a
	// double-click applies here.
	//
	// CMDK-07..10 (flag `cmdk.v2.preview`, issue #6090): a highlighted track
	// shows its overview in a strip above the bar (or inside the row with
	// `cmdk.v2.preview_inline`), ← and → preview it in the headphone cue in
	// phase with the master, and Enter opens a deck picker. With the flag off
	// none of it runs and the bar is slice 1's.
	//
	// CMDK-11..16 (flag `cmdk.v2.actions`, issue #6091): settings and commands
	// are results too. One typed character searches only them and recent loads;
	// tracks join from two. Holding Cmd (Ctrl off macOS) numbers the first nine
	// rows for a quick pick. The bar also answers `cmdk_open` / `cmdk_close`
	// through the dispatcher and shows agents its state in `query().cmdk`.
	//
	// CMDK-24 (issue #6848): typed search also lists library playlists to open.
	import { tick, untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import { revealPlaylist } from '$lib/rb/playlist-reveal.svelte';
	import { deckStates as decks, DECK_IDS, mixerState, pitchRanges } from '$lib/rb/audio-engine.svelte';
	import {
		searchGroups,
		rowKeys,
		warmHaystacks,
		cmdkGroupStarts,
		cmdkListSpliceAt,
		matchLists,
		matchedVia,
		queryTokens,
		fillSearchFields,
		highlightParts,
		rowMatches,
		type SearchableRow
	} from '$lib/rb/command-bar-groups';
	import {
		cmdkKeep,
		filterStates,
		type CmdkFilterPrefs,
		type CmdkFilterRow as FilterFields
	} from '$lib/rb/command-bar-filters';
	import type { FileAvailabilityStatus } from '$lib/rb/file-availability';
	import { clampSelection, cycleFocusIndex, defaultTargetDeck, stepSelection, waitForStemsSettled } from '$lib/rb/command-bar';
	import type { DeckId } from '$lib/rb/deck-id';
	import { filterForKeyCode, type LibraryFilterId } from '$lib/rb/library-filter-labels';
	import { libraryIndexIsCurrent, loadLibraryIndex, onLibraryIndexInvalidated, peekLibraryIndex } from '$lib/rb/library-index';
	import { createPickLoader, deckLoadMark, loadCommitted, settleForLoad, waitDeckIdle, type LoadGuard, type SettleMeta } from '$lib/rb/cmdk-load';
	import {
		bpmConfidenceIn,
		compatibleKeyChecked,
		computeNextOnlyRef,
		energyIn,
		nextOnlyReferenceDeck
	} from '$lib/rb/next-only-filter';
	import CmdkDeckRings from './cmdk/CmdkDeckRings.svelte';
	import CmdkFilterRow from './cmdk/CmdkFilterRow.svelte';
	import CmdkPreviewStrip from './cmdk/CmdkPreviewStrip.svelte';
	import PairEdge from './PairEdge.svelte';
	import CmdkDeckPicker from './cmdk/CmdkDeckPicker.svelte';
	import { cmdkActionsEnabled, cmdkAiEnabled, cmdkPairsEnabled, cmdkPreviewMode } from '$lib/rb/cmdk-flag';
	import { AI_DEBOUNCE_MS, AI_MIN_CHARS, aiTrack, audibleMaster, fitLabel, nextMode, rankByFit, shortlist, type CmdkMode } from '$lib/rb/cmdk-ai';
	import { checkCmdkAiKey, cmdkAi, cmdkAiAnswers, cmdkAiKeyKnown, requestCmdkAi, resetCmdkAi } from '$lib/rb/cmdk-ai-data.svelte';
	import { setCmdkAiToast } from '$lib/rb/cmdk-ai-toast';
	import { afterAlignedLoad, alignedPlan, alignedPlay, alignedSeek, beforeAlignedLoad, cmdkEntryKey, cmdkWireId, leadFor, pairBasis, ratioInRange, reanchorSelection, seekStalled, SEEK_CORRECTIONS, SEEK_SETTLE_MS, type PairCommand, type PairSnapshot } from '$lib/rb/cmdk-pairs';
	import { cmdkPairs, requestCmdkPairs } from '$lib/rb/cmdk-pairs-data.svelte';
	import { rankCommands, type CommandPick } from '$lib/palette/rank';
	import { performanceCommandsInScope, runCommand as runPerformanceCommand } from '$lib/keyboard/command-registry';
	import { commandedScopeOf } from '$lib/rb/commanded-keys';
	import { commandedKeyHost, selectedKnobIds } from '$lib/rb/knob-control.svelte';
	import {
		actionableSetting,
		actionTokens,
		commandCandidates,
		commandKey,
		paletteCommandRows,
		ejectAction,
		layoutGroups,
		noteRecentLoad,
		rankSettings,
		quickPickIndex,
		quickModifierHeld,
		quickPickModifier,
		settingNextValue,
		settingValueLabel,
		settingVerb,
		type CmdkAction,
		type CmdkPairTag,
		type CmdkAdapter,
		type CmdkEntry,
		type CmdkEntryGroup,
		type SettingAction,
		queryChars,
		tracksAllowed,
		registryCommandCandidates
	} from '$lib/rb/cmdk-actions';
	import { groupLabel } from '$lib/settings/catalog';
	import { effectiveHideTodo, filterSettings, hasSettingsQuery } from '$lib/settings/search';
	import { previewAssets, requestPreviewAssets, watchPreview } from '$lib/rb/cmdk-preview-data.svelte';
	import { createCuePreview, type CuePreviewCommand, type CuePreviewTrack } from '$lib/rb/cmdk-cue-preview.svelte';
	import type { PaletteIpcCommand } from '$lib/palette/commands';
	import { barPhase, downbeatTimes, snapStops } from '$lib/rb/cmdk-preview-grid';
	import { analysisSourceState } from '$lib/rb/analysis-source-state.svelte';
	import { shouldUseBeatgridFallback } from '$lib/rb/beatgrid-fallback';
	import { guardHolds, pickGuard, pickerAction, pickerArrow, pickerOrder, pickerStart, pickerStep, replaceGuard, type ReplaceGuard } from '$lib/rb/cmdk-deck-picker';
	import { previewCue } from '$lib/player/preview-cue.svelte';
	import type { AnlzBeat, AnlzData } from '$lib/rb/cmdk-preview-grid';
	import { effectiveWaveformDesign, type UiSkin } from '$lib/rb/ui-skin';
	import type { WaveformDesignPref } from '$lib/rb/waveform-design';
	import { ensureAnlzPrefetch, getAnlzEntry } from './wave/anlz-cache.svelte';
	import { ensureBeatgridFallback, getBeatgridFallbackEntry } from './wave/beatgrid-fallback-cache.svelte';
	import { beatgridFallbackGate, paintAnlzForRow, readyBeatgridFallback } from './wave/wave-row-beatgrid-fallback';
	import { sectionSpans } from './wave/sections';
	import { formatListTotalsLine, summarizeListTotals } from '$lib/rb/list-totals';

	/** What a row needs to be searched, filtered and loaded. */
	type CmdkRow = SearchableRow &
		Omit<FilterFields, 'file_availability'> & {
			file_exists: boolean | null;
			is_streaming: boolean | null;
			file_availability?: FileAvailabilityStatus;
			duration_ms?: number | null;
		};

	/** One selectable line: a track to load, or a library playlist to open. */
	type CmdkItem = { kind: 'track'; row: CmdkRow; pair?: CmdkPairTag | undefined } | { kind: 'list'; list: PlaylistNode } | { kind: 'action'; action: CmdkAction };

	let {
		rows,
		playlistTitle,
		hideBroken,
		playlists,
		openPlaylist,
		load,
		fetchMeta,
		reservation,
		soloVocals,
		uiPrefs,
		setAvailableOfflineFilter,
		setHideBrokenLinks,
		setNextOnlyFilter,
		setRemixesFilter,
		setVocalsFilter,
		runCommand,
		runCoordinated,
		deckLoadDispatches,
		faderWrites,
		deckCommands,
		deckPending,
		registerAdapter,
		aiToast,
		noteCommandPick
	}: {
		rows: readonly CmdkRow[];
		playlistTitle: string;
		/** The broken filter as the open pane resolved it (Missing Tracks is exempt). */
		hideBroken: boolean;
		playlists: readonly PlaylistNode[];
		/** CMDK-6848: opens the playlist as a click on its tree row does. */
		openPlaylist: (list: PlaylistNode) => void;
		/** The browser's load path for a settled row (a refusal returns normally). */
		load: (row: CmdkRow, deck: DeckId, stillNewest?: LoadGuard) => Promise<void>;
		/** The table's own retrying rb-meta fetch, to settle a pending pick. */
		fetchMeta: (stable_id: string) => Promise<SettleMeta>;
		/** The browser's reservations on a deck, which move before its load commits. */
		reservation: (deck: DeckId) => number;
		soloVocals: (deck: DeckId, settled: 'ready' | 'unavailable' | 'error' | 'timeout') => Promise<void>;
		/** The page passes the prefs store and its filter setters, so this bar
		 * does not import prefs.svelte itself (keeps its fan-in down). */
		uiPrefs: CmdkFilterPrefs & {
			waveform_design: WaveformDesignPref;
			ui_skin: UiSkin;
			theme: string;
			wave_palette: string;
			show_dev_ui: boolean;
			hide_todo_settings: boolean;
			cmdk_command_picks: readonly CommandPick[];
		};
		setAvailableOfflineFilter: (next: boolean) => void;
		setHideBrokenLinks: (next: boolean) => void;
		setNextOnlyFilter: (next: boolean) => void;
		setRemixesFilter: (next: boolean) => void;
		setVocalsFilter: (next: boolean) => void;
		/** The typed dispatcher's UI entry, passed in by the page so this bar does
		 * not import performance-ipc.svelte itself (the most-imported module). */
		runCommand: (
			command: CuePreviewCommand | PairCommand | { type: 'unload'; deck: DeckId } | PaletteIpcCommand,
			source?: 'user-ui' | 'agent-command'
		) => Promise<{ ok: true } | { ok: false; reason: string }>;
		/** Runs `work` holding these decks plus the sync scope for its whole run; `work`
		 * sends its commands through the run function it is given (CMDK-19). */
		runCoordinated: (
			decks: readonly DeckId[],
			work: (run: (command: PairCommand) => Promise<{ ok: true } | { ok: false; reason: string }>) => Promise<void>
		) => Promise<void>;
		/** Loads and unloads the dispatcher has sent to a deck so far (CMDK-16). */
		deckLoadDispatches: (deck: DeckId) => number;
		/** Fader writes the dispatcher has been sent for a deck so far, any value (CMDK-19). */
		faderWrites: (deck: DeckId) => number;
		/** Deck-queue commands other than loads and beat_sync dispatched to a deck so far; with 'beat_sync', its beat_sync commands (CMDK-19). */
		deckCommands: (deck: DeckId, kind?: 'beat_sync') => number;
		/** Commands the dispatcher still holds for a deck (CMDK-16). */
		deckPending: (deck: DeckId) => number;
		/** Registers the bar for cmdk_open / cmdk_close and query().cmdk (CMDK-15). */
		registerAdapter: (adapter: CmdkAdapter) => () => void;
		/** The red toast the AI search's no-key state raises (CMDK-21); the page wires `pushToast`. */
		aiToast: (message: string) => void;
		/** The page's frecency recorder (prefs), so this bar does not import prefs.svelte. */
		noteCommandPick: (key: string) => void;
	} = $props();

	/** Rows each group draws at once; the header count says when there are more. */
	const GROUP_LIMIT = 50;
	/** Playlists drawn at once, kept short so the collection group stays in reach. */
	const LIST_LIMIT = 5;

	let open = $state(false);
	let query = $state('');
	let selected = $state(0);
	let target = $state<DeckId>(1);
	let input = $state<HTMLInputElement | null>(null);
	let wrap = $state<HTMLDivElement | null>(null);
	let collection = $state<readonly CmdkRow[]>([]);
	let collectionError = $state<string | null>(null);
	/** Why the last picker pick was refused, shown under the deck picker. */
	let pickerNote = $state<string | null>(null);
	/** True while the library index has never loaded and its first load is in flight. */
	let collectionPending = $state(false);
	/** The exact row sets whose search strings are all built; a new set is cold until its warm-up ends. */
	let warmedFor = $state.raw<readonly (readonly CmdkRow[])[] | null>(null);

	const tokens = $derived(queryTokens(query));
	/** The settings search's words: the hint and Enter read the option the shown row was found by. */
	const settingTokens = $derived(actionTokens(query));
	const refSlices = $derived(
		DECK_IDS.map((d) => ({
			is_master: decks[d].is_master,
			playing: decks[d].playing,
			stable_id: decks[d].stable_id,
			key: decks[d].key,
			bpm: decks[d].bpm,
			pitch: decks[d].pitch,
			bpm_confidence: bpmConfidenceIn(decks[d].stable_id, collection),
			key_shift_semitones: decks[d].key_shift_semitones
		}))
	);
	const ref = $derived.by(() => {
		const r = computeNextOnlyRef(
			refSlices,
			compatibleKeyChecked(uiPrefs.compatible_filter)
		);
		if (r === null) return null;
		// SETPREP-11947: the reference track's MIK energy, for the energy rule.
		const deck = nextOnlyReferenceDeck(refSlices, compatibleKeyChecked(uiPrefs.compatible_filter));
		return { ...r, energy: energyIn(deck?.stable_id ?? null, collection) };
	});
	const states = $derived(filterStates(uiPrefs));
	/** Pane rows lack album and notes; the index row with the same id fills them for search. */
	const playlistRows = $derived(fillSearchFields(rows, collection));
	/**
	 * Warm only for the exact row sets on screen: a set published after the
	 * last warm-up (the index resolving, the pane reloading) is cold in the same
	 * update that publishes it, so no render searches it before it is warmed.
	 */
	const searchWarm = $derived(warmedFor !== null && warmedFor[0] === playlistRows && warmedFor[1] === collection);
	/**
	 * A typed search needs the library index: while its first load is in
	 * flight, or after a load failed, no groups and no "No tracks match" are
	 * shown, so a result never claims the collection was searched when it was
	 * not. Reopening the bar retries the load. It also waits for the idle
	 * warm-up, so a keystroke never builds the remaining haystacks itself
	 * (spec AC-P2); the search runs as soon as the last slice lands.
	 */
	const actionsOn = $derived(cmdkActionsEnabled());
	/** With settings and commands on, one typed character searches no tracks (CMDK-11). */
	const trackSearch = $derived(!actionsOn || tokens.length === 0 || tracksAllowed(query));
	const awaitingCollection = $derived(
		(collectionPending || collectionError !== null || !searchWarm) && tokens.length > 0 && trackSearch
	);
	/**
	 * The open pane's broken-filter exemption (Missing Tracks) covers its own
	 * rows only; the collection always follows the persisted preference.
	 */
	const keep = $derived({ playlist: cmdkKeep(uiPrefs, ref, hideBroken), collection: cmdkKeep(uiPrefs, ref) });
	const groups = $derived(
		awaitingCollection || !trackSearch
			? { playlist: { rows: [], total: 0 }, collection: { rows: [], total: 0 }, hiddenByFilters: 0 }
			: searchGroups(playlistRows, collection, query, keep, GROUP_LIMIT)
	);
	/** A playlist can hold one track twice (LIBM-03), so its rows are keyed by membership. */
	const playlistKeys = $derived(rowKeys(groups.playlist.rows));
	const lists = $derived(matchLists(playlists, query, LIST_LIMIT));
	const cmdkSearchTotalsLine = $derived.by(() => {
		if (!trackSearch || tokens.length === 0 || awaitingCollection) return null;
		const seen = new Set<string>();
		const rows: { duration_ms: number | null }[] = [];
		for (const row of [...groups.playlist.rows, ...groups.collection.rows]) {
			if (seen.has(row.stable_id)) continue;
			seen.add(row.stable_id);
			rows.push({ duration_ms: row.duration_ms ?? null });
		}
		const totals = summarizeListTotals(rows);
		if (totals.count === 0) return null;
		return formatListTotalsLine(totals);
	});

	// ---- CMDK-11..16: settings and commands as results ----

	/** The settings read/write module, loaded on the first open with the flag on (keeps prefs.svelte out of this file). */
	let settingsApi = $state.raw<typeof import('$lib/settings/apply') | null>(null);
	/** The settings module is still importing for this open: its results are not in the list yet, so Enter waits. */
	let settingsLoading = $state(false);
	/** Bumped by every open and close, so an open still importing settings knows it was superseded. */
	let barLife = 0;
	/** Tracks loaded on any deck while this page is open, most recent first. */
	let recentIds = $state<string[]>([]);
	/** Every track loaded this page session, unbounded: recentIds is display-limited and forgets old loads. */
	let playedIds = $state<string[]>([]);
	/** The quick-pick modifier is held: the first nine rows show their numbers. */
	let quickHeld = $state(false);
	/** A playing deck an eject waits on: confirm with Cmd/Ctrl+Enter or a click. */
	let ejectGuard = $state<ReplaceGuard | null>(null);
		/** The deck's command count when the eject guard was taken: a play queued after it changes what a confirm would do (Codex P1 BLOCKING, #6689). */
		let ejectCommands = 0;
	/** Bumped by every eject confirm, so a continuation still waiting on its deck installs nothing once a newer one started (Sol P1, #6689). */
	let ejectOp = 0;
	/** Why the last setting or command did not run, until the next one. */
	let actionError = $state<string | null>(null);
	const quickMod = quickPickModifier(typeof navigator === 'undefined' ? '' : navigator.platform);
	const quickLabel = quickMod === 'Meta' ? '⌘' : 'Ctrl ';

	const recentRows = $derived.by((): CmdkRow[] => {
		if (!actionsOn) return [];
		const byId = new Map<string, CmdkRow>();
		for (const row of collection) byId.set(row.stable_id, row);
		for (const row of playlistRows) if (!byId.has(row.stable_id)) byId.set(row.stable_id, row);
		const out: CmdkRow[] = [];
		for (const id of recentIds) {
			const row = byId.get(id);
			if (row !== undefined && rowMatches(row, tokens)) out.push(row);
		}
		return out;
	});
	const settingActions = $derived.by((): SettingAction[] => {
		const api = settingsApi;
		// filterSettings lists every setting for a query it cannot tokenize (`?`), so only a real word reaches it.
		if (!actionsOn || api === null || tokens.length === 0 || !hasSettingsQuery(query)) return [];
		const found = filterSettings(query, {
			hideTodo: effectiveHideTodo(uiPrefs),
			showDev: uiPrefs.show_dev_ui,
			group: null
		}).keyword;
		const out: SettingAction[] = [];
		for (const def of found) {
			if (!actionableSetting(def)) continue;
			const link = def.control.kind === 'link';
			if (!link && !api.isKnownSettingKey(def.id)) continue;
			const value = link || !api.isKnownSettingKey(def.id) ? '' : api.readSettingValue(def.id);
			out.push({
				kind: 'setting',
				id: def.id,
				label: def.label,
				path: `Settings > ${groupLabel(def.group)}`,
				def,
				value,
				next: settingNextValue(def, value, actionTokens(query))
			});
		}
		return rankSettings(out, query);
	});
	const commandActions = $derived(
		actionsOn
			? (() => {
					const dialScope = commandedScopeOf(selectedKnobIds());
					const rows = DECK_IDS.map((id) => ({
						id,
						loaded: decks[id].stable_id !== null,
						playing: decks[id].playing,
						is_master: decks[id].is_master,
						beat_sync_enabled: decks[id].beat_sync_enabled,
						title: decks[id].title,
						load_generation: decks[id].load_generation
					}));
					return [
						...commandCandidates(rows, query),
						...paletteCommandRows({ target, decks: rows }, query),
						...(dialScope === null ? [] : registryCommandCandidates(performanceCommandsInScope(dialScope)))
					];
				})()
			: []
	);
	// CMDK-9465: commands ranked VS Code style (fuzzy, word starts, runs), recent picks first (3-day half-life).
	const rankedCommands = $derived(
		rankCommands(commandActions, query, { key: commandKey, picks: uiPrefs.cmdk_command_picks })
	);
	// ---- CMDK-17..19: the master deck's pairs on open ----

	const pairsOn = $derived(actionsOn && cmdkPairsEnabled());
	/** The master deck, when it holds a track. */
	const masterDeck = $derived(DECK_IDS.find((id) => decks[id].is_master && decks[id].stable_id !== null) ?? null);
	const pairsState = $derived(cmdkPairs());
	/** The master whose pairs were last asked for: an open bar asks again only when the master changes. */
	let pairsMaster: string | null = null;
	// The master can change while the bar stays open, and its pairs follow it (Sol: the old answer was dropped and never re-asked).
	$effect(() => {
		if (!open || !pairsOn) return;
		const master = masterDeck === null ? null : decks[masterDeck].stable_id;
		if (master === pairsMaster) return;
		pairsMaster = master;
		untrack(() => requestCmdkPairs(master));
	});
	/** The master's pairs as bar rows, while the answer is for the track the master holds now. */
	const pairRows = $derived.by((): { row: CmdkRow; aligned: boolean; basis: string | undefined; snapshot: PairSnapshot | null }[] => {
		const state = pairsState;
		if (!pairsOn || masterDeck === null || state.status !== 'ready' || state.master !== decks[masterDeck].stable_id) return [];
		const byId = new Map<string, CmdkRow>();
		for (const row of collection) byId.set(row.stable_id, row);
		for (const row of playlistRows) if (!byId.has(row.stable_id)) byId.set(row.stable_id, row);
		const out: { row: CmdkRow; aligned: boolean; basis: string | undefined; snapshot: PairSnapshot | null }[] = [];
		for (const pair of state.pairs) {
			const row = byId.get(pair.stable_id);
			if (row !== undefined) {
				const basis = pair.snapshot === null ? undefined : pairBasis(state.master, masterDeck, pair.snapshot);
				out.push({ row, aligned: pair.snapshot !== null, basis, snapshot: pair.snapshot });
			}
		}
		return out;
	});
	/** Aligned loads in flight, oldest first. Each clears only its own entry, so the bar stays open until the last one settles (Codex, #6691). */
	let pairLoads = $state<Array<{ id: number; title: string; deck: DeckId }>>([]);
	let pairLoadSeq = 0;
	/** The newest aligned load in flight: the bar stays open so a refusal can be read. */
	const pairLoading = $derived(pairLoads.length > 0 ? pairLoads[pairLoads.length - 1] : null);
	/** The footer's pick state for agents: the last pick's failure, else an aligned pair loading, in the footer's own order (Codex P2 BLOCKING, #6691). */
	const cmdkOperation = $derived.by((): { status: 'loading'; title: string; deck: DeckId } | { status: 'error'; message: string } | null => {
		if (actionError !== null) return { status: 'error', message: actionError };
		if (pairLoading !== null) return { status: 'loading', title: pairLoading.title, deck: pairLoading.deck };
		return null;
	});
	/**
	 * The fader a deck had before the aligned picks in flight on it dropped it to 0. An overlapping pick
	 * reads the 0 the first one wrote, so the baseline is shared while any of them is in flight: the
	 * last pick to fail gives back the deck's own fader, not a 0 it never owned (Codex P2 BLOCKING, #6691).
	 */
	const preludeBaseline = new Map<DeckId, { fader: number; picks: number }>();
	/**
	 * Aligned picks that landed on a deck and are still in their corrections. A pick that fails after a
	 * predecessor landed does not give the fader back: that would make the partner the predecessor
	 * already holds at 0 abruptly audible (Codex P1 BLOCKING, #6691).
	 */
	const landedPicks = new Map<DeckId, number>();
	/** The load generation the latest landed pick left on each deck: a later load replaces it (Codex r4280465902). */
	const landedGeneration = new Map<DeckId, number>();

	// ---- CMDK-20..22: Tab for AI search via JEV ----

	const aiOn = $derived(cmdkAiEnabled());
	/** Plain text search, or AI search: Tab flips it while the flag is on. */
	let mode = $state<CmdkMode>('plain');
	const aiState = $derived(cmdkAi());
	const aiWords = $derived(query.trim());
	/** Already played: every load this session (not only the recent few), and whatever a deck holds now (CMDK-20). */
	const aiPlayed = $derived(
		new Set([...playedIds, ...DECK_IDS.map((id) => decks[id].stable_id).filter((id): id is string => id !== null)])
	);
	/**
	 * What JEV is asked about: tracks with a file that mix into the master and were not played.
	 * "With a file" is "not known missing": a cold library serves most rows AVAILABILITY_PENDING
	 * (file_exists null), and a pick of one is settled through rb-meta before it loads (CMDK-04),
	 * so leaving them out would empty the shortlist on exactly the libraries that need it.
	 */
	const aiShortlist = $derived(
		mode === 'ai'
			? shortlist(
					collection.filter((row) => row.file_exists !== false),
					masterDeck === null ? null : audibleMaster(decks[masterDeck]),
					aiPlayed
				)
			: []
	);
	/** The shortlist ranked by JEV, only while the answer is for the words in the box now. */
	// Only over a current index: a held index (an ingest or edit since the answer) or a failed
	// refresh could list tracks that changed or are gone, so the answer waits for the refresh.
	/**
	 * The AI status the bar and the query snapshot report. A ready answer for other words is still asking
	 * while the query is long enough to ask; below the threshold nothing is pending, so it is idle
	 * (Codex P2 BLOCKING, #6692).
	 */
	// AI mode reports the collection it waits on too: a failed index is an error, and a ready answer
	// that the refreshing index has not settled is still working, never ready (Codex NON-BLOCKING, #6692).
	const aiShown = $derived(
		mode === 'ai' && collectionError !== null && aiState.status !== 'no-key'
			? 'error'
			: mode === 'ai' && collectionPending && aiState.status === 'ready'
				? 'working'
				: (aiState.status === 'ready' && (aiState.words !== aiWords || !cmdkAiAnswers(aiWords, aiShortlist.map(aiTrack)))) ||
					  (aiState.status === 'error' && aiState.words !== aiWords)
					? queryChars(aiWords) < AI_MIN_CHARS
						? 'idle'
						: 'working'
					: aiState.status
	);
	const aiRanked = $derived(
		mode === 'ai' &&
			aiState.status === 'ready' &&
			aiState.words === aiWords &&
			!collectionPending &&
			collectionError === null &&
			cmdkAiAnswers(aiWords, aiShortlist.map(aiTrack))
			? rankByFit(aiShortlist, aiState.scores)
			: []
	);
	const aiFit = $derived(new Map(aiRanked.map((hit) => [hit.row.stable_id, hit.p])));
	function fitOf(id: string): { fit: number } | Record<string, never> {
		const p = aiFit.get(id);
		return p === undefined ? {} : { fit: p };
	}
	// The key's answer is what the ask waits on: a key that resolves after the first pass schedules it, and only that flip re-runs
	// this effect, so no status change in between costs a call (Codex P2, #6692).
	const aiKeyKnown = $derived(cmdkAiKeyKnown());
	// One JEV call per pause in typing, never per keystroke; no key asks nothing. A held
	// index that is not current yet asks nothing either: the refresh would change the
	// shortlist and ask the same words again, a second paid call over stale tracks.
	$effect(() => {
		if (mode !== 'ai' || !aiKeyKnown || queryChars(aiWords) < AI_MIN_CHARS || collectionPending || collectionError !== null) return;
		const words = aiWords;
		const tracks = aiShortlist.map(aiTrack);
		const timer = setTimeout(() => {
			if (untrack(() => cmdkAiKeyKnown())) requestCmdkAi(words, tracks);
		}, AI_DEBOUNCE_MS);
		return () => clearTimeout(timer);
	});

	function setMode(next: CmdkMode): void {
		mode = next;
		selected = 0;
		touched = false;
		picker = null;
		actionError = null;
		if (next === 'ai') checkCmdkAiKey();
		else resetCmdkAi();
	}

	const layout = $derived.by((): { groups: CmdkEntryGroup<CmdkRow>[]; keepTyping: boolean } => {
		if (mode === 'ai') {
			return {
				groups: aiRanked.length > 0 ? [{ id: 'ai', entries: aiRanked.map((hit) => ({ kind: 'track', row: hit.row })) }] : [],
				keepTyping: false
			};
		}
		if (actionsOn) {
			return layoutGroups<CmdkRow>({
				query,
				recent: recentRows,
				playlist: groups.playlist,
				collection: groups.collection,
				commands: rankedCommands,
				settings: settingActions,
				pairs: pairRows,
				idOf: (row) => row.stable_id,
				commandsRanked: true
			});
		}
		const out: CmdkEntryGroup<CmdkRow>[] = [];
		for (const id of ['playlist', 'collection'] as const) {
			const group = groups[id];
			if (group.rows.length > 0) out.push({ id, total: group.total, entries: group.rows.map((row) => ({ kind: 'track', row })) });
		}
		return { groups: out, keepTyping: false };
	});
	/** Inserted library-playlist rows count toward flat indices only in plain mode with actions off. */
	const cmdkListRowCount = $derived(mode === 'plain' && !actionsOn ? lists.rows.length : 0);
	/** Each group's first index into `flat`. */
	const groupStart = $derived(cmdkGroupStarts(layout.groups, cmdkListRowCount));
	/** Every selectable row, in the order drawn. */
	const flat = $derived.by((): CmdkItem[] => {
		const entries = layout.groups.flatMap((group) => group.entries) as CmdkItem[];
		if (cmdkListRowCount === 0) return entries;
		const playlistTracks = cmdkListSpliceAt(layout.groups);
		const before = entries.slice(0, playlistTracks);
		const after = entries.slice(playlistTracks);
		const listPart = lists.rows.map((list) => ({ kind: 'list' as const, list }));
		return [...before, ...listPart, ...after];
	});
	const selectedEntry = $derived(flat[selected]);
	/** The highlighted row when it is a track. */
	const selectedTrack = $derived(selectedEntry?.kind === 'track' ? selectedEntry.row : undefined);

	// Every load on any deck, from any path, is a recent load.
	const seenGeneration: Partial<Record<DeckId, number>> = {};
	$effect(() => {
		for (const id of DECK_IDS) {
			const stable_id = decks[id].stable_id;
			const generation = decks[id].load_generation;
			if (stable_id === null || seenGeneration[id] === generation) continue;
			seenGeneration[id] = generation;
			recentIds = noteRecentLoad(
				untrack(() => recentIds),
				stable_id
			);
			if (!untrack(() => playedIds).includes(stable_id)) playedIds = [...untrack(() => playedIds), stable_id];
		}
	});

	/** Resolves with why the settings API could not load (also shown in `actionError`), or null once it is in. */
	function loadSettingsApi(): Promise<string | null> {
		if (settingsApi !== null) return Promise.resolve(null);
		// On first use: apply.ts reads and writes the shared prefs store.
		return import('$lib/settings/apply')
			.then((mod) => {
				settingsApi = mod;
				return null;
			})
			.catch((exc: unknown) => {
				actionError = `Settings are unavailable here: ${exc instanceof Error ? exc.message : String(exc)}`;
				return actionError;
			});
	}

	/** Flip, set or open a setting, or run a command. A playing deck's eject asks first. */
	/**
	 * Returns this action's own failure (null when it went through), so the caller reads its result
	 * rather than the bar's shared error, which another pick may have set or cleared meanwhile
	 * (Codex r4279465812). The message still shows in the bar, but only while that bar is open.
	 */
	async function runAction(action: CmdkAction, confirmed: boolean, rowStillPicked: () => boolean = () => true, source: 'user-ui' | 'agent-command' = 'user-ui'): Promise<string | { asked: DeckId } | null> {
		const session = barSession;
		if (barSession === session) actionError = null;
		const fail = (message: string): string => {
			if (barSession === session) actionError = message;
			return message;
		};
		if (action.kind === 'setting') {
			if (action.def.control.kind === 'link') {
				const href = action.def.control.href;
				closeBar();
				await goto(href);
				return null;
			}
			const api = settingsApi;
			// A pick never reads as done while nothing changed: the settings module still loading is a
			// refusal the caller sees, not a silent success (Codex r4278070876).
			if (api === null) return fail(`${action.label} did not change: settings are still loading; pick it again`);
			try {
				// An enum value that is not one of its options is refused here with its reason (Sol r4279823899).
				const next = settingNextValue(action.def, action.value, actionTokens(query));
				if (api === null || next === null) return null;
				api.applySettingChange(action.id, next);
			} catch (exc) {
				return fail(`${action.label} did not change: ${exc instanceof Error ? exc.message : String(exc)}`);
			}
			return null;
		}
		const command = action.command;
		if (command.type === 'open_settings') {
			if (source === 'user-ui') noteCommandPick(commandKey(action));
			closeBar();
			const { openSettings } = await import('$lib/settings/overlay.svelte');
			openSettings(command.query);
			return null;
		}
		if (command.type === 'performance') {
			const host = commandedKeyHost();
			if (host.selectedIds().length === 0) return fail(`${action.label} did not run: no dial or fader is selected`);
			try {
				runPerformanceCommand(command.id, { timeStamp: performance.now(), commanded: host });
			} catch (exc) {
				return fail(`${action.label} did not run: ${exc instanceof Error ? exc.message : String(exc)}`);
			}
			if (source === 'user-ui') noteCommandPick(commandKey(action));
			closeBar();
			return null;
		}
		if (command.type === 'palette') {
			const run = command.run;
			if (run.via === 'ui') {
				closeBar();
				const { showHotkeysOverlay } = await import('$lib/components/rb/hotkeys/hotkeys-overlay.svelte');
				showHotkeysOverlay();
				return null;
			}
			const result = await runCommand(run.command, source);
			if (result.ok) {
				closeBar();
				return null;
			}
			return fail(`${action.label} did not run (${result.reason})`);
		}
		// A newer eject, or a confirm, that starts while this one waits takes the bar: this one must not
		// overwrite its guard or unload after the wait (Codex P2, #6689).
		const op = ++ejectOp;
		// A confirm answers the track the deck holds as it is given, so it is pinned now: a load still
		// queued for the deck lands first, and a confirm that load outdated ejects nothing, from the
		// guarded dialog and a direct cmdk_pick alike (Codex r4278070869).
		const asked = confirmed ? replaceGuard(command.deck, decks[command.deck], deckLoadDispatches(command.deck), deckCommands(command.deck)) : null;
		// A play already queued for this deck (IPC, MIDI) runs before an unload sent now would, so a
		// paused deck read here could be playing when the eject lands: let its queue drain, then
		// decide whether to ask (Codex r4276912170).
		// The track the pick read: a load that lands while the drain waits replaces it, and the eject must not unload that one (Codex BLOCKING, #6691).
		const picked = decks[command.deck].load_generation;
		if (deckPending(command.deck) > 0) {
			// A load dispatched during this wait (IPC or MIDI) lands paused and would read as the track the row named, so
			// the eject must not unload it: the row referred to the track on the deck before the wait (Codex P1, #6689).
			const loads = deckLoadDispatches(command.deck);
			const drained = await waitDeckIdle(
				() => deckPending(command.deck),
				(ms) => new Promise((resolve) => setTimeout(resolve, ms))
			);
			if (!drained) return fail(`Deck ${command.deck} is still busy; it was not ejected`);
			// A newer eject or confirm took the bar while this one waited: it answers for the deck now.
			if (op !== ejectOp) return `Deck ${command.deck}: a newer eject superseded this one; it was not ejected`;
			if (deckLoadDispatches(command.deck) !== loads) return fail(`Deck ${command.deck} took another load while it was busy; it was not ejected`);
			if (decks[command.deck].load_generation !== picked) return fail(`Deck ${command.deck} took another load before the eject; it was not ejected`);
			// A queued unload can empty the deck during the drain without moving the load count, so the eject
			// would unload an empty deck again. An emptied deck is left as it is (CMDK-14, Sol P1, #6689).
			if (!decks[command.deck].stable_id) return null;
		}
		// The drain can outlast the bar: a close during the wait ends the eject, and so does a close and reopen with the
		// same row, which the row check below cannot tell apart (Codex P2 BLOCKING, #6691).
		if (barSession !== session || !open) return fail(`The Cmd K bar closed before deck ${command.deck} could be ejected; it was not ejected`);
		// The drain can outlast the row: an open or query change while it waited may have put another row at this index,
		// and the eject must not run on the row the agent read (Codex P2 BLOCKING, #6691).
		if (!rowStillPicked()) return fail(`The row changed while deck ${command.deck} was busy; read query() and pick again`);
		if (asked !== null && !guardHolds(asked, decks[command.deck], deckLoadDispatches(command.deck), deckCommands(command.deck))) {
			return fail(`Deck ${command.deck} took another load before the eject; it was not ejected (confirm again to eject the track there now)`);
		}
		if (ejectAction(decks[command.deck], confirmed) === 'ask') {
			// The confirmation belongs to this bar: an eject that waited out a queued command must not
			// install its dialog in a bar reopened meanwhile (Codex r4280339710).
			if (barSession === session) {
				ejectGuard = replaceGuard(command.deck, decks[command.deck], deckLoadDispatches(command.deck), deckCommands(command.deck));
				ejectCommands = deckCommands(command.deck);
			}
			return { asked: command.deck };
		}
		if (barSession === session) ejectGuard = null;
		// The eject carries the source of the pick that asked for it: an agent's confirmed eject is an agent stop (PLAY-18).
		const result = await runCommand({ type: 'unload', deck: command.deck }, source);
		if (result.ok && source === 'user-ui') noteCommandPick(commandKey(action));
		return result.ok ? null : fail(`Deck ${command.deck} did not eject (${result.reason})`);
	}

	/** Enter, a click or a quick pick on any row. */
	function activate(entry: CmdkItem | undefined, shift: boolean): void {
		if (entry === undefined) return;
		// Until the settings module lands, its results are missing from the list, so Enter would pick the next
		// row by mistake (Sol P1, #6689). It waits instead.
		if (settingsLoading) {
			actionError = 'Settings are still loading; press Enter again in a moment';
			return;
		}
		if (entry.kind === 'list') {
			void openPlaylistRow(entry.list);
			return;
		}
		if (entry.kind === 'action') {
			void runAction(entry.action, false);
			return;
		}
		if (previewMode !== 'off') openPicker(entry.row, shift, entry.pair);
		else void choose(entry.row, shift, entry.pair, false);
	}

	function openPlaylistRow(list: PlaylistNode): void {
		closeBar();
		openPlaylist(list);
		revealPlaylist(list.playlist_id);
	}

	// The no-key toast goes through the page's pushToast, so neither this bar nor "/" first paint loads it.
	$effect(() => setCmdkAiToast(aiToast));

	// Agents open, fill and read the bar through the dispatcher (cmdk_open / cmdk_close / cmdk_pick / cmdk_mode, query().cmdk).
	// The newest cmdk_open owns the query: an older call that resumes after it, from the open it awaited, does nothing.
	let openTurn = 0;
	// The settings load the newest open is waiting on. A second open while the bar is open joins it, so it gets the same failure (Sol P1, #6689).
	let openSettings: Promise<string | null> | null = null;
	$effect(() =>
		registerAdapter({
			open: async (text) => {
				const turn = ++openTurn;
				const failure = open ? await openSettings : await openBar();
				// A settings failure is reported to every opener, a superseded one included, as well as shown on the bar (Sol P1, #6689).
				if (failure !== null) throw new Error(failure);
				if (turn !== openTurn) return;
				if (!open) throw new Error('the Cmd K bar closed before it opened; cmdk_open again');
				if (text !== null) {
					query = text;
					selected = 0;
					touched = false;
					picker = null;
					// The reopen replaces what an open eject confirmation was asking about, so that guard and any pending eject go (Sol P1, #6689).
					ejectGuard = null;
					ejectOp += 1;
				}
			},
			close: () => closeBar(),
			pick: async (index, deck, confirm, rowId, targetAt, vocalsOnly, source) => {
				if (!open) throw new Error('the Cmd K bar is closed; cmdk_open it first');
				// An omitted `deck` loads to the target, so a caller that read a target in the snapshot is refused once it moved
				// (Codex P2 BLOCKING, #6691). Checked again after the guard's wait, since the target can move during it.
				const targetMoved = () => targetAt !== null && target !== targetAt;
				if (targetMoved()) throw new Error(`the target deck is now ${target}, not ${targetAt}; read query() and pick again`);
				const entry = flat[index];
				if (entry === undefined) throw new Error(`there is no row ${index}; the bar shows ${flat.length}`);
				if (entry.kind === 'list') {
					openPlaylistRow(entry.list);
					return;
				}
				// Rows can arrive after query() was read (pair rows land asynchronously): a pick pinned to the
				// row it read is refused once that index holds another row (Codex r4279465774); a pair row has its
				// own id, so a plain row replaced by the same track's pair row is refused too (Codex r4279717126).
				const at = cmdkWireId(entry);
				if (rowId !== null && at !== rowId) throw new Error(`row ${index} is now ${at}, not ${rowId}; read query() and pick again`);
				selected = index;
				if (entry.kind === 'action') {
					// What this action reports is its own result, never the shared guard or error (Codex r4280465910).
					const outcome = await runAction(entry.action, confirm, () => flat[index] !== undefined && cmdkWireId(flat[index]) === at, source);
					if (typeof outcome === 'object' && outcome !== null) throw new Error(`deck ${outcome.asked} is playing; pick again with confirm to eject it`);
					if (outcome !== null) throw new Error(outcome);
					return;
				}
				// The same guard the deck picker applies: never the master, a playing deck only when confirmed,
				// read once the deck's queue has landed, and a confirm only for the load it was sent against.
				const id = deck ?? target;
				const seen = { loads: -1, commands: -1 };
				// The bar this pick was read from. The guard can wait on a busy deck, and a close or reopen in that
				// wait makes the pick belong to a bar that is gone (Codex P2 BLOCKING, #6691).
				const session = barSession;
				const refused = await pickGuard(
					id,
					{
						deck: () => {
							const at = pickerDecks.find((candidate) => candidate.id === id);
							return at === undefined ? undefined : { ...at, stable_id: decks[id].stable_id, load_generation: decks[id].load_generation };
						},
						loads: () => deckLoadDispatches(id),
						commands: () => deckCommands(id),
						pending: () => deckPending(id),
						idle: () =>
							waitDeckIdle(
								() => deckPending(id),
								(ms) => new Promise((resolve) => setTimeout(resolve, ms))
							)
					},
					confirm,
					seen
				);
				// A close during the wait is the bar going away too, not only a reopen (Codex P2 BLOCKING, #6691).
				if (barSession !== session || !open) throw new Error('the Cmd K bar closed before this pick ran; pick again');
				// The pinned row is read again after the wait: pair rows can land or turn over meanwhile, and the
				// pick must act on the row whose id the agent read (Codex P2 BLOCKING, #6691).
				if (flat[index] === undefined || cmdkWireId(flat[index]) !== at) throw new Error(`row ${index} changed while the deck was busy; read query() and pick again`);
				if (targetMoved()) throw new Error(`the target deck moved to ${target} while deck ${id} was busy; read query() and pick again`);
				if (refused !== null) throw new Error(refused);
				// A load dispatched after the guard's read and before the pick starts below is one the guard
				// never saw; nothing awaits from here until choose begins the pick (Codex r4278944613).
				if (deckLoadDispatches(id) !== seen.loads) throw new Error(`deck ${id} took another load before this pick; pick again`);
				// A seek, tempo or play sent in the same window is newer than the verdict too: pickLoad would adopt it as the baseline.
				if (deckCommands(id) !== seen.commands) throw new Error(`deck ${id} was sent another command before this pick; pick again`);
				target = id;
				picker = null;
				// A refusal left from an earlier pick is not this pick's.
				actionError = null;
				// A pick that did not land (a newer load won, the load path refused the row, or the pair
				// did not align) is a failed pick for the caller too, never a success (Codex r4277653901).
				const failed = await choose(entry.row, vocalsOnly, entry.pair, confirm);
				if (failed !== null) throw new Error(failed);
			},
			snapshot: () => ({
				open,
				query,
				selected,
				// The deck an omitted `deck` loads to, so an agent names the same deck the visible rings show (Codex P2 BLOCKING, #6691).
				target,
				rows: flat.map((entry) =>
					entry.kind === 'list'
						? { kind: 'playlist' as const, id: String(entry.list.playlist_id), label: entry.list.name }
						: entry.kind === 'track'
							? {
									kind: 'track' as const,
									id: cmdkWireId(entry),
									label: entry.row.title ?? '(untitled)',
									...(entry.pair === undefined ? {} : { pair: entry.pair.aligned ? ('aligned' as const) : ('loose' as const) }),
									...fitOf(entry.row.stable_id)
								}
							: entry.action.kind === 'setting'
								? {
										kind: 'setting' as const,
										// The id a pick validates against, so a row id read here is accepted by cmdk_pick (Codex P2 BLOCKING, #6692).
										id: cmdkWireId(entry),
										label: entry.action.label,
										// The value the row shows and what Enter does to it, as the row's own hint reads (Codex P2 BLOCKING, #6691).
										value: settingValueLabel(entry.action.def, entry.action.value),
										action: settingVerb(entry.action.def, entry.action.value, settingTokens)
									}
								: // The id a pick validates, as for a setting, so an Open settings row read with its query is accepted (Codex P2 BLOCKING, #6691).
									{ kind: 'command' as const, id: cmdkWireId(entry), label: entry.action.label }
				),
				mode,
				ai: aiShown,
				// CMDK snapshot: these two spreads must stay outside the `rows: flat.map` callback so
				// unit tests that slice to `})\\n\\t\\t})` catch the real snapshot end, not an inner `})`.
				// The aligned pair's pending load and the last pick's failure, as the footer shows them (Codex r4280465903).
				...(open && cmdkOperation !== null ? { operation: cmdkOperation } : {}),
				// The deck picker or eject confirmation holding the bar, so an agent reads the choice the DJ is making (Codex, #6691).
				...(open && picker !== null
					? { dialog: { kind: 'deck-picker' as const, deck: picker.sel, title: picker.row.title ?? '(untitled)', confirming: picker.guard !== null } }
					: open && ejectGuard !== null
						? { dialog: { kind: 'eject' as const, deck: ejectGuard.id } }
						: {}),
				// The collection index and its search warmup, as the bar shows them, so rows read while the index loads
				// or after it failed are not taken for a search with no matches (Codex r4280115661).
				...(open
					? {
							collection:
								collectionError !== null
									? { status: 'error' as const, message: collectionError }
									: { status: collectionPending || !searchWarm ? ('loading' as const) : ('ready' as const) }
						}
					: {}),
				// The master's pairs lookup, as the bar shows it, so a failed read is not taken for no pairs (Codex r4279717136).
				...(pairsOn && masterDeck !== null
					? {
							pairs:
								pairsState.status === 'error'
									? { status: 'error' as const, message: pairsState.message }
									: { status: pairsState.status === 'ready' && pairsState.master === decks[masterDeck].stable_id ? ('ready' as const) : ('loading' as const) }
						}
					: {})
			}),
			setMode: (next) => {
				if (!open) throw new Error('the Cmd K bar is closed; cmdk_open it first');
				if (!aiOn) throw new Error('AI search is off: turn on flag cmdk.v2.ai');
				if (next !== mode) setMode(next);
			}
		})
	);

	function flatEntryKey(entry: CmdkItem): string {
		if (entry.kind === 'list') return `pl:${entry.list.playlist_id}`;
		return cmdkEntryKey(entry);
	}

	// Rows that arrive after the bar opened (the pairs group, a refreshed index) keep the
	// highlight on the entry it was on, for the same query: the index alone would slide it onto
	// another track, and Enter would load that one (Codex r4277873858).
	let anchor: string | null = null;
	let anchoredList: unknown = null;
	let anchoredQuery = '';
	$effect.pre(() => {
		const list = flat;
		const at = selected;
		if (list !== anchoredList && query === anchoredQuery) {
			const moved = reanchorSelection(anchor, list.map(flatEntryKey), at);
			anchoredList = list;
			if (moved !== at) {
				selected = moved;
				return;
			}
		}
		anchoredList = list;
		anchoredQuery = query;
		anchor = list[at] === undefined ? null : flatEntryKey(list[at]);
	});

	// A refreshed index can shorten the list under a highlight further down it;
	// keep the highlight on a real row so Enter always acts on what is shown.
	$effect(() => {
		const clamped = clampSelection(selected, flat.length);
		if (clamped !== selected) selected = clamped;
	});

	// ---- CMDK-07..10: preview strip, cue preview, deck picker ----

	/** A row must be rested on this long before its overview is asked for (spec: 90 ms). */
	const PREVIEW_DWELL_MS = 90;
	const previewMode = $derived(cmdkPreviewMode());
	/** True once the user moved onto a row (↑↓, the pointer, or ← →); typing clears it. */
	let touched = $state(false);
	/** The deck picker: the track Enter chose, its vocals-only flag, the picked row, and a pending confirm. */
	// The selection is a deck, not a row index: the order moves when the master changes.
	let picker = $state<{ row: CmdkRow; vocalsOnly: boolean; sel: DeckId; guard: ReplaceGuard | null; pair?: CmdkPairTag | undefined } | null>(null);
	/** When the dwell asked for the overview, for the first-paint readout. */
	let dwellAt = 0;
	/** The row the dwell asked for; a paint counts toward its first-paint time only while that row is shown. */
	let dwellId: string | null = null;
	let firstPaintMs = $state<number | null>(null);

	/** The track the strip and the cue preview are about: the picker's, else the highlighted row. */
	const previewRow = $derived(
		previewMode === 'off' ? null : (picker?.row ?? (touched ? (selectedTrack ?? null) : null))
	);
	const previewId = $derived(previewRow?.stable_id ?? null);
	const previewAnlzEntry = $derived(previewId === null ? undefined : getAnlzEntry(previewId));
	const previewAnlz = $derived.by((): AnlzData | null => {
		const entry = previewAnlzEntry;
		return entry !== undefined && entry.status === 'ready' ? entry.data : null;
	});
	const previewAnlzErrorCode = $derived(
		previewAnlzEntry !== undefined && previewAnlzEntry.status === 'error' ? previewAnlzEntry.code : null
	);
	const previewFallbackGate = $derived(
		beatgridFallbackGate({
			anlzErrorCode: previewAnlzErrorCode,
			anlz: previewAnlz,
			effectiveSource: analysisSourceState.features.beatgrid
		})
	);
	/** The row the dwell last fired for; null from the moment the highlight moves. */
	let dwelled = $state<string | null>(null);
	$effect(() => {
		const id = previewId;
		if (id !== null && dwelled === id && shouldUseBeatgridFallback(previewFallbackGate)) ensureBeatgridFallback(id);
	});
	const previewBeatgridFallback = $derived(
		readyBeatgridFallback(previewId, previewFallbackGate, getBeatgridFallbackEntry)
	);
	const previewPaintAnlz = $derived(paintAnlzForRow(previewAnlz, previewBeatgridFallback));
	const previewGridPending = $derived.by(() => {
		if (previewId === null) return false;
		if (previewAnlzEntry === undefined || previewAnlzEntry.status === 'loading') return true;
		if (!shouldUseBeatgridFallback(previewFallbackGate)) return false;
		const fallbackEntry = getBeatgridFallbackEntry(previewId);
		return fallbackEntry === undefined || fallbackEntry.status === 'loading';
	});
	const previewData = $derived(previewId === null ? undefined : previewAssets(previewId));
	const previewDuration = $derived(
		previewRow?.duration_ms != null && previewRow.duration_ms > 0 ? previewRow.duration_ms / 1000 : null
	);
	const waveDesign = $derived(effectiveWaveformDesign(uiPrefs.waveform_design, uiPrefs.ui_skin));
	/** The preview strip's repaint key: the three prefs that swap the --rb-wave-* vars. */
	const wavePaletteKey = $derived(`${uiPrefs.theme}|${uiPrefs.wave_palette}|${uiPrefs.ui_skin}`);

	/** Downbeats per beat array, built once per /anlz payload. */
	const downbeatMemo = new WeakMap<readonly AnlzBeat[], number[]>();
	function downbeatsOf(anlz: AnlzData): number[] {
		const beats = anlz.beatgrid.beats;
		let hit = downbeatMemo.get(beats);
		if (hit === undefined) {
			// An untrusted grid's bar numbers are not derived from (Sol P1, #6687): the preview gets no downbeats and says why.
			hit = anlz.beatgrid.bar_phase_untrusted === true ? [] : downbeatTimes(beats);
			downbeatMemo.set(beats, hit);
		}
		return hit;
	}

	const cueTrack = $derived.by((): CuePreviewTrack | null => {
		if (previewRow === null) return null;
		const downbeats = previewPaintAnlz === null ? [] : downbeatsOf(previewPaintAnlz);
		const vocals =
			previewData?.status === 'ready' && (previewData.vocals.status === 'rekordbox' || previewData.vocals.status === 'demucs')
				? previewData.vocals.regions
				: [];
		return {
			stable_id: previewRow.stable_id,
			downbeats,
			stops: previewPaintAnlz === null ? [] : snapStops(downbeats, sectionSpans(previewPaintAnlz.phrases), vocals),
			duration_s: previewDuration,
			bpm: previewRow.bpm,
			// /anlz and, when its grid is empty, /beatgrid-fallback are one pending read.
			gridPending: previewGridPending,
			untrusted: previewPaintAnlz?.beatgrid.bar_phase_untrusted === true
		};
	});

	const cue = createCuePreview({
		dispatch: async (command) => (await runCommand(command)).ok,
		masterPhase: () => {
			const master = DECK_IDS.find((d) => decks[d].is_master && decks[d].playing);
			if (master === undefined) return null;
			// A playing master whose phase cannot be read is not "no master": the preview refuses
			// rather than starting knowingly out of phase (Sol r4280385227).
			const anlz = decks[master].anlz;
			const phase = anlz === null ? null : barPhase(downbeatsOf(anlz), decks[master].position_ms / 1000);
			return phase ?? 'unknown';
		},
		sounding: (stable_id) => previewCue.stable_id === stable_id && previewCue.playing,
		now: () => performance.now()
	});
	$effect(() => cue.setTrack(cueTrack));

	// Ask for the overview only after a rest on the row, so arrowing past rows fetches nothing.
	$effect(() => {
		const id = previewId;
		dwelled = null;
		watchPreview(null);
		// A paint from here until this row's own dwell is not its ask-to-paint time (Sol review of 8e53c84f1).
		dwellAt = 0;
		dwellId = null;
		firstPaintMs = null;
		if (id === null) return;
		const timer = setTimeout(() => {
			dwelled = id;
			watchPreview(id);
			dwellAt = performance.now();
			dwellId = id;
			firstPaintMs = null;
			requestPreviewAssets(id);
		}, PREVIEW_DWELL_MS);
		return () => {
			clearTimeout(timer);
			watchPreview(null);
		};
	});
	// Sections, cues and the grid come second: the /anlz read is the larger one and,
	// sent alongside, measurably delayed the strip's first paint. It waits for this row's
	// own dwell too, so a row whose strip is still cached asks nothing before 90 ms
	// (Sol r4278366206). Untracked: on a cache hit ensureAnlzPrefetch READS the entry and
	// WRITES it back with a fresh LRU stamp, so a tracked call reruns this effect on its own
	// write until Svelte aborts with effect_update_depth_exceeded (strip-anlz-prefetch.ts says
	// more). The cmdk-preview-cue e2e caught it here: the loop dropped the cue preview's state.
	$effect(() => {
		const id = previewId;
		if (id !== null && dwelled === id && previewData !== undefined && previewData.status !== 'loading')
			untrack(() => ensureAnlzPrefetch(id));
	});

	function onPreviewPainted(): void {
		if (firstPaintMs === null && dwellAt > 0 && dwellId !== null && dwellId === previewId) firstPaintMs = performance.now() - dwellAt;
	}

	const cuePlayhead = $derived(
		previewId !== null && previewCue.stable_id === previewId && previewCue.playing && previewDuration !== null
			? previewCue.position_ms / 1000 / previewDuration
			: null
	);
	// The cue's status counts only while its preview sounds: a voice that ended or was stopped elsewhere shows nothing (Sol P2, #6687).
	const cueSounding = $derived(cue.state.started && previewCue.playing && previewCue.stable_id === previewId);
	const cueAnchor = $derived.by((): number | null => {
		if (!cueSounding || cueTrack === null || previewDuration === null) return null;
		const at = cueTrack.downbeats[cue.state.anchor];
		return at === undefined ? null : at / previewDuration;
	});
	const cueMessage = $derived(
		cue.state.message !== '' && cueSounding
			? `${cue.state.message} · in the cue`
			: cue.state.message !== ''
				? cue.state.message
				: '← → to play it in the cue'
	);

	const pickerDecks = $derived(
		pickerOrder(
			DECK_IDS.map((id) => ({
				id,
				title: decks[id].title,
				artist: decks[id].artist,
				loaded: decks[id].stable_id !== null,
				playing: decks[id].playing,
				is_master: decks[id].is_master
			}))
		)
	);

	/** Where the picker's selected deck sits in the current order. */
	const pickerSel = $derived(picker === null ? -1 : pickerDecks.findIndex((deck) => deck.id === picker?.sel));

	function openPicker(row: CmdkRow, vocalsOnly: boolean, pair?: CmdkPairTag): void {
		picker = { row, vocalsOnly, sel: pickerDecks[pickerStart(pickerDecks, target)]?.id ?? target, guard: null, pair };
		pickerNote = null;
	}

	/** Load to the picker's deck at `index`, asking first over a playing deck. */
	function pickerLoad(index: number, confirmed: boolean): void {
		const p = picker;
		const deck = pickerDecks[index];
		if (p === null || deck === undefined) return;
		// A command still queued for the deck (a play from IPC or MIDI) is not in its state yet, so a pick read against a
		// paused deck could be approved while that play lands and the unload runs after it. The count moves at dispatch;
		// refuse, and the DJ picks again once the deck settles (Codex P1 BLOCKING, #6687).
		if (deckPending(deck.id)) {
			pickerNote = `Deck ${deck.id} is still busy; pick again once it settles`;
			return;
		}
		pickerNote = null;
		const action = pickerAction(deck, confirmed);
		if (action === 'refuse') return;
		if (action === 'ask') {
			// A load still queued for the deck lands first, so the question names the track that is
			// really there and a confirm never replaces one the DJ was not asked about (Codex r4278029265).
			if (deckPending(deck.id) > 0) {
				const session = barSession;
				void settledGuard(deck.id, session).then((guard) => {
					if (guard !== null && picker === p && barSession === session) p.guard = guard;
				});
				return;
			}
			p.guard = replaceGuard(deck.id, decks[deck.id], deckLoadDispatches(deck.id), deckCommands(deck.id));
			return;
		}
		target = deck.id;
		picker = null;
		void choose(p.row, p.vocalsOnly, p.pair, confirmed);
	}

	/**
	 * A confirm found the deck moved on: let loads still queued for it land first, so the
	 * question asked again names the track that is really there (Codex r4277027005).
	 */
	async function settledGuard(id: DeckId, session: number): Promise<ReplaceGuard | null> {
		const drained = await waitDeckIdle(
			() => deckPending(id),
			(ms) => new Promise((resolve) => setTimeout(resolve, ms))
		);
		// A queue that never drains is not asked about: a guard taken now would name the old
		// track, and a confirm of it would act behind the load still queued (Codex r4277285054).
		// The error is the bar's only while the same bar is open: a bar closed or reopened since
		// the wait began must not show it (Codex P2 BLOCKING, #6691).
		if (!drained) {
			if (barSession === session) actionError = `Deck ${id} is still busy; pick again once it settles`;
			return null;
		}
		return replaceGuard(id, decks[id], deckLoadDispatches(id), deckCommands(id));
	}

	async function confirmGuard(): Promise<void> {
		const p = picker;
		const guard = p?.guard ?? null;
		if (p === null || guard === null) return;
		const index = pickerDecks.findIndex((deck) => deck.id === guard.id);
		p.guard = null;
		if (index < 0) return;
		// The deck took another load while the guard was open: the confirm answered a
		// question about a track that is gone, so ask again about what is there now, playing
		// or not. Only a confirm of the load it asked about goes ahead.
		if (!guardHolds(guard, decks[guard.id], deckLoadDispatches(guard.id), deckCommands(guard.id))) {
			const session = barSession;
			const again = await settledGuard(guard.id, session);
			// A newer guard the DJ opened while this one settled stays: only an empty slot takes the answer (Codex P2, #6689).
			if (again !== null && picker === p && barSession === session && p.guard === null) p.guard = again;
			return;
		}
		pickerLoad(index, true);
	}

	/** Keys while the picker shows; true when the key was the picker's. */
	function pickerKey(event: KeyboardEvent): boolean {
		const p = picker;
		if (p === null) return false;
		// Tab keeps the bar's wrapping focus cycle while the picker is open (CMDK-04; Sol review of 8e53c84f1).
		if (event.key === 'Tab') {
			cycleFocus(event.shiftKey);
			return true;
		}
		if (p.guard !== null) {
			// Only Cmd/Ctrl+Enter (or the button) confirms; plain Enter and Esc keep playing.
			if (event.key === 'Enter' && quickModifierHeld(event, quickMod)) void confirmGuard();
			else if (event.key === 'Escape' || event.key === 'Enter' || event.key === 'x' || event.key === 'X') p.guard = null;
			return true;
		}
		const arrow = pickerArrow(event);
		if (arrow === 'ArrowUp' || arrow === 'ArrowDown') {
			p.sel = pickerDecks[pickerStep(pickerDecks, pickerSel, arrow === 'ArrowDown' ? 1 : -1)]?.id ?? p.sel;
			return true;
		}
		if (arrow === 'ArrowLeft' || arrow === 'ArrowRight') {
			void cue.jump(arrow === 'ArrowRight' ? 1 : -1, event.shiftKey);
			return true;
		}
		const digit = event.altKey ? /^Digit([1-4])$/.exec(event.code) : null;
		if (digit !== null) {
			const at = pickerDecks.find((deck) => deck.id === Number(digit[1]) && !deck.is_master);
			if (at !== undefined) p.sel = at.id;
			return true;
		}
		if (event.key === 'Enter') {
			pickerLoad(pickerSel, false);
			return true;
		}
		if (event.key === 'Escape') {
			picker = null;
			return true;
		}
		// Typing (or deleting) goes back to the results and edits the search.
		if (event.key === 'Backspace' && event.target !== input) {
			// Focus is on a picker row, where the input never sees the key: delete from the
			// search here, as the window handler types into it (Sol review of 8e53c84f1).
			picker = null;
			query = [...query].slice(0, -1).join('');
			selected = 0;
			touched = false;
			input?.focus();
			return true;
		}
		if (event.key === 'Backspace' || (event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey)) {
			picker = null;
			return false;
		}
		// Editing keys belong to the query input: Delete and Cmd/Ctrl+A, C, V, X, Z pass through to it, and the query
		// change dismisses the picker as it does for typing (Codex P2, #6687).
		if (event.key === 'Delete' || ((event.metaKey || event.ctrlKey) && 'acvxz'.includes(event.key.toLowerCase()))) return false;
		return true;
	}

	function deckList(): { id: DeckId; loaded: boolean; is_master: boolean }[] {
		return DECK_IDS.map((id) => ({ id, loaded: decks[id].stable_id !== null, is_master: decks[id].is_master }));
	}

	/** Bumped per warm-up so newer rows (or a reopen) stop the older run. */
	let warmRun = 0;
	/**
	 * Build the search strings of the open pane's rows, then the collection's,
	 * in 8 ms slices between frames: never on the open tick (AC-P1) nor on a
	 * keystroke (AC-P2). All Tracks can be 50,000 pane rows on its own.
	 */
	function warmSearch(lists: readonly (readonly CmdkRow[])[]): void {
		const run = ++warmRun;
		let list = 0;
		let at = 0;
		const step = (): void => {
			if (run !== warmRun || !open) return;
			const until = performance.now() + 8;
			while (list < lists.length && performance.now() < until) {
				at = warmHaystacks(lists[list], at, () => performance.now() < until);
				if (at >= lists[list].length) {
					list += 1;
					at = 0;
				}
			}
			if (list < lists.length) setTimeout(step, 0);
			else warmedFor = lists;
		};
		setTimeout(step, 0);
	}
	// Re-warm whenever the bar opens or either row set changes while it is open.
	$effect(() => {
		if (open) warmSearch([playlistRows, collection]);
	});

	// Each open's request; only the latest one may publish, so a slower request
	// for an older index generation never overwrites a newer one (or its error a success).
	let collectionRequest = 0;
	function loadCollection(): void {
		const request = ++collectionRequest;
		// A reopen is the retry: drop the old error so the hint reads "Loading" while it runs.
		collectionError = null;
		const held = peekLibraryIndex();
		if (held !== null) {
			collection = held.items as readonly CmdkRow[];
		}
		// A held index from before an ingest or edit still shows, but a typed search
		// waits for the current generation rather than report a stale "No tracks match".
		collectionPending = held === null || !libraryIndexIsCurrent();
		loadLibraryIndex().then(
			(index) => {
				if (request !== collectionRequest) return;
				collection = index.items as readonly CmdkRow[];
				// The library changed while this request ran: show these rows, but keep
				// a typed search waiting and ask for the current generation.
				if (!libraryIndexIsCurrent() && open) {
					loadCollection();
					return;
				}
				collectionError = null;
				collectionPending = false;
			},
			(error: unknown) => {
				if (request !== collectionRequest) return;
				collectionError = error instanceof Error ? error.message : String(error);
				collectionPending = false;
			}
		);
	}

	// A library change while the bar is open (an ingest, an edit, a track event)
	// re-asks for the index, so the open bar never keeps searching the old rows.
	$effect(() => {
		if (!open) return;
		return onLibraryIndexInvalidated(() => loadCollection());
	});

	/** Moves on every open, so work begun in one opening never closes the next (Codex r4276688941). */
	let barSession = 0;

	/** Opens the bar at once, so typing lands in the search. Resolves with a settings failure, or null. */
	async function openBar(): Promise<string | null> {
		barSession += 1;
		actionError = null;
		const life = ++barLife;
		query = '';
		selected = 0;
		touched = false;
		picker = null;
		ejectGuard = null;
		target = defaultTargetDeck(deckList());
		pairLoads = [];
		pairsMaster = masterDeck === null ? null : decks[masterDeck].stable_id;
		if (pairsOn) requestCmdkPairs(pairsMaster);
		open = true;
		settingsLoading = actionsOn && settingsApi === null;
		openSettings = actionsOn ? loadSettingsApi() : Promise.resolve(null);
		loadCollection();
		await tick();
		input?.focus();
		const failure = await openSettings;
		// A close or a newer open supersedes this one, so it must not publish its state (Sol P1, #6689).
		if (life !== barLife) return null;
		settingsLoading = false;
		return failure;
	}

	function closeBar(): void {
		barLife += 1;
		// An eject still waiting on its deck's queue must not resume into the closed bar (Sol P1, #6689).
		ejectOp += 1;
		settingsLoading = false;
		open = false;
		picker = null;
		touched = false;
		ejectGuard = null;
		quickHeld = false;
		cue.stop();
		if (mode !== 'plain') setMode('plain');
	}

	function toggleFilter(id: LibraryFilterId): void {
		const next = !states[id];
		if (id === 'broken') setHideBrokenLinks(!next);
		else if (id === 'remixes') setRemixesFilter(next);
		else if (id === 'vocals') setVocalsFilter(next);
		else if (id === 'compatible') setNextOnlyFilter(next);
		else setAvailableOfflineFilter(next);
		selected = 0;
		input?.focus();
	}

	function pickDeck(id: DeckId): void {
		target = id;
		input?.focus();
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'k') {
			event.preventDefault();
			if (open) closeBar();
			else {
				quickHeld = actionsOn && quickModifierHeld(event, quickMod);
				void openBar();
			}
			return;
		}
		if (open && actionsOn && picker === null && ejectGuard === null) {
			// CMDK-13: holding the modifier numbers the first nine rows; modifier+digit picks one.
			if (event.key === quickMod) quickHeld = true;
			const index = quickPickIndex(event, quickMod);
			if (index !== null) {
				event.preventDefault();
				event.stopPropagation();
				if (index < flat.length) {
					selected = index;
					activate(flat[index], false);
				}
				return;
			}
		}
		// Keys typed in the moment between Cmd K and the input taking focus (or
		// while focus sits on a filter or deck button) belong to the search.
		if (!open || event.target === input) return;
		// The deck picker's keys (WASD included) come before search typing, so a click on a
		// picker row that moved focus off the input still steers the picker (Sol r4278366223).
		if (previewMode !== 'off' && picker !== null && wrap?.contains(event.target as Node) && pickerKey(event)) {
			event.preventDefault();
			event.stopPropagation();
			return;
		}
		if (event.metaKey || event.ctrlKey || event.altKey || event.key.length !== 1) return;
		// Space on a focused filter icon or deck ring is the button's own activation.
		if (event.key === ' ' && event.target instanceof HTMLButtonElement) return;
		event.preventDefault();
		event.stopPropagation();
		query += event.key;
		// Typing edits the search, exactly as in the input: the purposeful highlight and the
		// picker clear, so arrows edit the new query rather than steer a preview (Sol r4278366215).
		selected = 0;
		touched = false;
		picker = null;
		input?.focus();
	}

	// Settles a pending pick, then loads it unless its deck moved on meanwhile.
	// The ports are read at call time, so the page's panel is always the live one.
	const pickLoad = createPickLoader<CmdkRow, DeckId>(
		(stable_id) => fetchMeta(stable_id),
		(row, deck, stillNewest) => loadCommitted(() => decks[deck], row.stable_id, () => load(row, deck, stillNewest)),
		(deck) => deckLoadMark(decks[deck], reservation(deck)),
		{
			loadsDispatched: (deck) => deckLoadDispatches(deck),
			commandsDispatched: (deck) => deckCommands(deck),
			pending: (deck) => deckPending(deck),
			idle: (deck) =>
				waitDeckIdle(
					() => deckPending(deck),
					(ms) => new Promise((resolve) => setTimeout(resolve, ms))
				)
		}
	);

	/**
	 * Resolves null once the pick's load landed, else why it did not: this pick's own result, never
	 * read back from the bar's shared error, which a newer opening of the bar does not show.
	 * `confirmed`: the DJ confirmed replacing a playing deck (the picker's guard, or cmdk_pick's confirm).
	 */
	async function choose(row: CmdkRow | undefined, vocalsOnly: boolean, pair: CmdkPairTag | undefined, confirmed: boolean): Promise<string | null> {
		if (row === undefined) return 'there is no row to pick';
		const deck = target;
		if (pair?.aligned === true) return await loadAligned(row, deck, confirmed, pair.basis, vocalsOnly);
		closeBar();
		// A pending pick's disk truth is settled first, and the pick is dropped if the
		// deck moved on meanwhile; a dropped or refused pick never solos vocals.
		const dispatched = await pickLoad(row, deck);
		// The bar's shared error may belong to an aligned pick that overlaps this one: the plain load reports
		// no text of its own, so this pick's refusal is its own message (Codex r4280465911).
		if (!dispatched) return `deck ${deck} did not load it (a newer load won, or the load was refused)`;
		if (!vocalsOnly || decks[deck].stable_id !== row.stable_id) return null;
		// The solo is for THIS load: a reload of the same track (IPC, MIDI, AutoPlay) during the
		// wait moves the engine's load generation and reads as another track, so it is never soloed.
		const landed = decks[deck].load_generation;
		// And a load dispatched since, still queued behind the stems wait, is a newer track too:
		// the solo would run after it lands (Codex r4276857706). The count moves at dispatch.
		const loads = deckLoadDispatches(deck);
		const settled = await waitForStemsSettled(
			() => ({ stable_id: decks[deck].load_generation === landed ? decks[deck].stable_id : null, status: decks[deck].stems.status }),
			row.stable_id
		);
		if (settled !== 'stale' && decks[deck].load_generation === landed && deckLoadDispatches(deck) === loads) await soloVocals(deck, settled);
		return null;
	}

	/** The master as it plays now, for an aligned load. */
	function masterNow(): { deck: DeckId; position_ms: number; ratio: number; playing: boolean } | null {
		if (masterDeck === null) return null;
		const m = decks[masterDeck];
		return { deck: masterDeck, position_ms: m.position_ms, ratio: m.pitch, playing: m.playing };
	}

	/**
	 * CMDK-19: an aligned pair loads at its captured offset against the master
	 * as it plays now, with the fader at 0, through the browser's own load path
	 * and the dispatcher. The bar stays open until it lands, so a refusal (the
	 * master is off the captured overlap, the deck moved on) is read where it
	 * was asked for, and closes once it has.
	 */
	async function loadAligned(row: CmdkRow, deck: DeckId, confirmed: boolean, basis: string | undefined, vocalsOnly: boolean): Promise<string | null> {
		// The bar this pick was asked in, read before any wait: a pick that outlives its bar (closed and
		// reopened during the rb-meta or deck wait) must not show its state in the new bar (Codex r4279465810).
		const session = barSession;
		if (barSession === session) actionError = null;
		// Shown in the bar, and returned to the caller as this pick's own result.
		const fail = (message: string): string => {
			if (barSession === session) actionError = message;
			return message;
		};
		// The pick starts the moment the DJ picks, before any wait: a load, play, cue or seek
		// dispatched to the deck from here on is newer than this pick and holds it back, rather
		// than being taken for the baseline (Codex r4277480917).
		let start = pickLoad.begin(deck);
		// What the deck already has queued (a load, a play) runs before anything here reads it or
		// lowers its fader, so a queued play never starts under this pick's 0 (Codex r4277285048).
		if (deckPending(deck) > 0) {
			const drained = await waitDeckIdle(
				() => deckPending(deck),
				(ms) => new Promise((resolve) => setTimeout(resolve, ms))
			);
			if (!drained) {
				return fail(`Deck ${deck} is still busy; it was not loaded`);
			}
			// Only what was queued before the pick ran: its mark is the one this pick holds.
			const resumed = pickLoad.resume(deck, start);
			if (resumed === null) {
				return fail(`Deck ${deck} was sent another command while it was busy; it was not loaded`);
			}
			start = resumed;
		}
		const pair = pairRows.find((candidate) => candidate.row.stable_id === row.stable_id);
		const master = masterNow();
		// The picker loads the pair it showed: a master that changed since then has a different alignment under the same
		// partner, so the stale pick is refused rather than loaded against the new snapshot (Codex P2 BLOCKING, #6691).
		if (basis !== undefined && pair !== undefined && pair.basis !== basis) return fail('The master moved since this pair was picked; pick it again');
		if (pair === undefined || pair.snapshot === null || master === null) {
			return fail('This pair has no captured alignment any more; load it from the search instead');
		}
		if (deck === master.deck) {
			return fail(`Deck ${deck} is the master; pick another deck for its pair`);
		}
		// The fader goes to 0 before the load: never on a playing deck the DJ did not confirm,
		// which Enter with the deck picker off would otherwise reach.
		if (decks[deck].playing && !confirmed) {
			return fail(`Deck ${deck} is playing; pick a deck that is not, or confirm replacing it in the deck picker`);
		}
		const snap = pair.snapshot;
		const early = alignedPlan(snap, master, row.duration_ms ?? null);
		if (!early.ok) {
			return fail(`Not loaded: ${early.reason}`);
		}
		// The captured rate must fit the deck's pitch range before anything on it changes: refused
		// later, by the tempo command, it would leave the partner loaded in place of the old track
		// at fader 0 (Codex r4276826024).
		if (!ratioInRange(early.ratio, pitchRanges[deck])) {
			const pct = ((early.ratio - 1) * 100).toFixed(1);
			return fail(`Not loaded: the captured tempo needs ${pct}% on deck ${deck}, outside its +-${pitchRanges[deck]}% pitch range`);
		}
		// The deck's own command count when the pick starts, moved only by this pair's own sends
		// from here on. A play, cue or seek sent to the deck by anyone else (even one queued behind
		// this load, which runs before the hold is claimed) moves it, and the pair stops rather than
		// act on a deck the DJ has already taken (Codex r4277100142).
		let expected = start.used;
		const taken = (): boolean => deckCommands(deck) !== expected;
		const pairId = ++pairLoadSeq;
		if (barSession === session) pairLoads = [...pairLoads, { id: pairId, title: row.title ?? '(untitled)', deck }];
		// Given back if this track never lands, so a newer load on the deck is not left muted. Read
		// again in the deck's hold right before the 0, so a move made while rb-meta settled is the one
		// given back (Codex r4279465781).
		let priorFader = mixerState.channels[deck].fader;
		let inPrelude = false;
		let holdsLanded = false;
		// The fader write count right after this pick's own write to 0, or null when it did not
		// write or another write raced it. While the count still reads it, no newer write exists:
		// not one of the same value, nor one still awaiting the engine (Codex r4276134970, r4276471326).
		let mine: number | null = null;
		const faderUntouched = (): boolean => mine !== null && faderWrites(deck) === mine;
		const masterTrack = decks[master.deck].stable_id;
		// A same-track reload of the master is a new load the DJ may have taken over with: the corrections end on it too (Codex r4280465905).
		const masterGeneration = decks[master.deck].load_generation;
		/**
		 * Whether the pair still aligns against the master as it is now: the same deck and track, inside
		 * the captured overlap, at a rate the deck's pitch range can take. Checked last before the load
		 * goes out, so a stale alignment keeps the deck's track rather than replacing it (Codex r4278029270).
		 */
		// Why the pair no longer aligns against the master as it is now, or null while it does: the reason
		// the held load reports, so a refusal here reads as the overlap or tempo that failed (Sol P1).
		const alignRefusal = (now: ReturnType<typeof masterNow>): string | null => {
			if (now === null) return 'there is no master deck any more';
			if (now.deck !== master.deck || decks[master.deck].stable_id !== masterTrack || decks[master.deck].load_generation !== masterGeneration) return 'the master changed';
			const plan = alignedPlan(snap, now, row.duration_ms ?? null);
			if (!plan.ok) return plan.reason;
			if (!ratioInRange(plan.ratio, pitchRanges[deck])) return `the captured tempo now needs ${((plan.ratio - 1) * 100).toFixed(1)}% on deck ${deck}, outside its +-${pitchRanges[deck]}% pitch range`;
			return null;
		};
		let holdReason: string | null = null;
		/** Why this pair may no longer act on its decks, or null while both still hold what it aligned. */
		const lost = (): string | null => {
			if (decks[deck].stable_id !== row.stable_id || decks[deck].load_generation !== landed) return `stopped: another load took deck ${deck}`;
			if (masterDeck !== master.deck || decks[master.deck].stable_id !== masterTrack || decks[master.deck].load_generation !== masterGeneration) return 'loaded with the fader at 0, but stopped: the master changed';
			return null;
		};
		// The engine's committed-load count once this pick lands: a later load of the same track
		// (IPC, MIDI, AutoPlay) moves it, and is not this pair's to retune or seek.
		let landed: number | null = null;
		// The plan the first hold below played against, for the corrections that follow it.
		let aligned = null as Extract<ReturnType<typeof alignedPlan>, { ok: true }> | null;
		let masterUsed = 0;
		let syncUsed = 0;
		// The pair's own Beat Sync sends to the partner, counted apart from anyone else's.
		let ownSync = 0;
		// Every command after the load is sent only while the deck still holds THIS load, no one
		// else has sent the deck a command, and the master is still the same deck and track.
		// They run inside a hold of both decks plus the sync scope (runCoordinated), so no seek,
		// tempo or play from MIDI, IPC or AutoPlay reaches either deck between the steps: one hold
		// for the load's own steps, then one per corrective seek.
		let via: ((command: PairCommand) => Promise<{ ok: true } | { ok: false; reason: string }>) | null = null;
		const send = async (command: PairCommand): Promise<{ ok: true } | { ok: false; reason: string }> => {
			if (via === null) throw new Error('an aligned command was sent outside its coordinated hold');
			if (taken()) throw new Error(`stopped: deck ${deck} was sent another command`);
			const gone = lost();
			if (gone !== null) throw new Error(gone);
			// Play only while the fader is still this pick's 0: a write since then could make the
			// partner heard from its load point before the offset seek lands (Codex r4276471318).
			if (command.type === 'play' && !faderUntouched()) throw new Error(`loaded, but left paused: deck ${deck}'s fader moved during the load`);
			if (command.type !== 'beat_sync') expected += 1;
			else ownSync += 1;
			return await via(command);
		};
		try {
			// The row is settled from rb-meta before the fader moves, so no metadata wait sits
			// between the fader at 0 and the load: a newer play that drops the pick then gets its
			// fader back at once, not after a cold rb-meta retry (Codex r4278983114).
			const settledRow = await settleForLoad(row, fetchMeta);
			// The freshness check and the fader's 0 are one step under the deck's hold, so no play
			// queued after the pick runs between them; one dispatched during the hold runs after it,
			// and the check right after the hold sees it before anything waits.
			await runCoordinated([deck], async (run) => {
				const fresh = pickLoad.resume(deck, start);
				if (fresh === null || deckPending(deck) > 0) throw new Error(`deck ${deck} was sent another command before the pair loaded; it was not loaded`);
				start = fresh;
				const shared = preludeBaseline.get(deck);
				priorFader = shared?.fader ?? mixerState.channels[deck].fader;
				preludeBaseline.set(deck, { fader: priorFader, picks: (shared?.picks ?? 0) + 1 });
				inPrelude = true;
				for (const command of beforeAlignedLoad(deck)) {
					const before = faderWrites(deck);
					const result = await run(command);
					if (!result.ok) throw new Error(`deck ${deck} fader did not go to 0 (${result.reason})`);
					if (command.type === 'fader') mine = faderWrites(deck) === before + 1 ? before + 1 : null;
				}
			});
			if (taken() || deckLoadDispatches(deck) !== start.sent) throw new Error(`deck ${deck} was sent another command during the fader prelude; it was not loaded`);
			// A fader write that raced the prelude (by hand, MIDI or IPC) is unscoped, so taken() never sees it,
			// and it left the deck's fader off this pick's 0: loading now could be heard from the unaligned load
			// point, so the pick stops before the load and the newer fader stays (Codex r4280152479).
			if (!faderUntouched()) throw new Error(`deck ${deck}'s fader moved before the pair loaded; it was not loaded`);
			// The master can move during the fader prelude and the rb-meta wait (its tempo, its track, or
			// past the captured overlap): the load is held back, keeping what the deck has, once the pair
			// no longer aligns against it (Codex r4277873847, r4278029270). A fader write in that wait is
			// unscoped too, so the last-moment guard also holds the load once the fader is off this pick's 0
			// (Codex r4280328376).
			start = {
				...start,
				fits: () => {
					holdReason = faderUntouched() ? alignRefusal(masterNow()) : `deck ${deck}'s fader moved before the pair loaded`;
					return holdReason === null;
				}
			};
			holdReason = null;
			const dispatched = await pickLoad(settledRow, deck, start);
			if (!dispatched || decks[deck].stable_id !== row.stable_id) {
				if (holdReason !== null) throw new Error(`Not loaded: ${holdReason}`);
				throw new Error(`deck ${deck} did not load it (the deck moved on, or the load was refused)`);
			}
			landed = decks[deck].load_generation;
			holdsLanded = true;
			landedPicks.set(deck, (landedPicks.get(deck) ?? 0) + 1);
			landedGeneration.set(deck, landed);
			// The counts as the hold is queued: they move at dispatch, so a master command or a Beat Sync
			// sent while the hold waits is counted now but runs after it, and must still end the
			// corrections. A baseline read when the hold starts would take it for the pair's own
			// (Codex r4278029260, Codex r4285745629).
			const masterAt = deckCommands(master.deck);
			const syncAt = deckCommands(deck, 'beat_sync');
			await runCoordinated([deck, master.deck], async (run) => {
				via = run;
				if (taken()) throw new Error(`loaded with the fader at 0, but stopped: deck ${deck} was sent another command during the load`);
				// A master or Beat Sync command counted while the hold waited runs before this callback's pair work,
				// so the pair is refused rather than sought and started under that newer command (Codex BLOCKING, #6692).
				if (deckCommands(master.deck) !== masterAt || deckCommands(deck, 'beat_sync') !== syncAt) {
					throw new Error(`deck ${deck} or its master was sent a command while the pair waited; pick again`);
				}
				// The master kept playing through the load: align against where it is now.
				const now = masterNow();
				const plan = now === null ? ({ ok: false, reason: 'there is no master deck any more' } as const) : alignedPlan(snap, now, decks[deck].duration_ms);
				if (!plan.ok) throw new Error(`loaded with the fader at 0, but not aligned: ${plan.reason}`);
				// And once more after the load, before the tempo goes out: a master tempo change during
				// the decode is refused here, not by the engine after the deck already took it.
				if (!ratioInRange(plan.ratio, pitchRanges[deck])) {
					throw new Error(`loaded with the fader at 0, but not aligned: the master's tempo moved, and the captured tempo now needs ${((plan.ratio - 1) * 100).toFixed(1)}% on deck ${deck}, outside its +-${pitchRanges[deck]}% pitch range`);
				}
				for (const command of afterAlignedLoad(deck, plan)) {
					const result = await send(command);
					if (!result.ok) throw new Error(`loaded with the fader at 0, but ${command.type} failed (${result.reason})`);
				}
				// The offset last, against the master as it is now, so the commands above do not leave it behind.
				const last = masterNow();
				const final = last === null ? plan : alignedPlan(snap, last, decks[deck].duration_ms);
				if (!final.ok) throw new Error(`loaded with the fader at 0, but not aligned: ${final.reason}`);
				const seek = await send(alignedSeek(deck, final));
				if (!seek.ok) throw new Error(`loaded with the fader at 0, but seek failed (${seek.reason})`);
				// Play only once the paused partner sits on its offset: nothing before it can be heard.
				const go = alignedPlay(deck, final);
				if (go !== null) {
					const started = await send(go);
					if (!started.ok) throw new Error(`loaded and aligned with the fader at 0, but play failed (${started.reason})`);
				}
				via = null;
				aligned = final;
				// The master's own command count as the hold was taken: a tempo, seek or play sent to the
				// master since ends the corrections, which would otherwise seek the partner against a
				// master it no longer matches (Codex r4277613896).
				masterUsed = masterAt;
				// And the partner's beat_sync count as the hold was taken, plus the pair's own Beat Sync
				// off: the DJ turning it back on would re-phase the partner over the captured tempo and
				// offset, so that ends the corrections too (Codex r4277769141).
				syncUsed = syncAt + ownSync;
			});
			// A seek on a playing deck lands behind where it was sent (about 120 ms on the fixture,
			// steady after). Once the deck state has caught up, corrective seeks led by the lag
			// measured so far put it on the captured offset; two are enough to land inside the floor.
			// The wait runs with both decks released, so the DJ's play, cue or seek is never queued
			// behind it; each seek takes the hold again only for itself, and any command the deck
			// was sent meanwhile ends the corrections there (Codex r4277100150).
			let lead = 0;
			// One more settled readback after the last correction verifies it: a pair still off by
			// more than the floor then is reported, never closed as landed (Codex r4277769146).
			for (let round = 0; round <= SEEK_CORRECTIONS && aligned !== null && aligned.play; round++) {
				await new Promise((resolve) => setTimeout(resolve, SEEK_SETTLE_MS));
				let done = false;
				await runCoordinated([deck, master.deck], async (run) => {
					// A fader write since the pick's 0 (by hand, MIDI or IPC, during play or the settle) can make
					// the partner heard, and its writes are unscoped, so taken() never sees them: the DJ has the
					// deck, and no corrective seek jumps it while it may be audible (Codex r4280308822).
					if (taken() || !faderUntouched() || deckCommands(master.deck) !== masterUsed || deckCommands(deck, 'beat_sync') !== syncUsed) {
						done = true;
						return;
					}
					// An unload or load of either deck is not a deck command, and a correction that ends
					// without a seek never reaches send's own check: the pair reports it rather than
					// closing as landed (Codex r4277873853).
					const gone = lost();
					if (gone !== null) throw new Error(gone);
					via = run;
					const now2 = masterNow();
					const want = now2 === null ? null : alignedPlan(snap, now2, decks[deck].duration_ms);
					if (want !== null && want.ok && seekStalled(want.position_ms, decks[deck].position_ms)) {
						const off = Math.abs(Math.round(want.position_ms - decks[deck].position_ms));
						throw new Error(`loaded with the fader at 0, but not aligned: it reads ${off} ms off its captured offset after the seek`);
					}
					// The master's own clock can carry it off the captured overlap during the settle
					// wait, with no command to either deck: that is a failed alignment, reported with
					// the plan's reason, never closed as landed (Codex r4278201290).
					if (want === null) throw new Error('loaded with the fader at 0, but not aligned: the master deck is no longer set');
					if (!want.ok) throw new Error(`loaded with the fader at 0, but not aligned: ${want.reason}`);
					const lag = leadFor(want.position_ms, decks[deck].position_ms);
					if (lag === 0) {
						done = true;
					} else if (round === SEEK_CORRECTIONS) {
						throw new Error(`loaded with the fader at 0, but not aligned: it is still ${Math.abs(lag)} ms off its captured offset after ${SEEK_CORRECTIONS} corrections`);
					} else {
						lead += lag;
						const nudge = await send(alignedSeek(deck, { ...want, position_ms: Math.max(0, want.position_ms + lead) }));
						if (!nudge.ok) throw new Error(`loaded with the fader at 0, but seek failed (${nudge.reason})`);
					}
					via = null;
				});
				if (done) break;
			}
			if (barSession === session) pairLoads = pairLoads.filter((p) => p.id !== pairId);
			// Only the last aligned load to settle closes the bar: another pick still loading keeps it open, and so
			// does a refusal from an earlier overlapping pick, which closing would drop (Codex P2 BLOCKING, #6691).
			if (open && barSession === session && pairLoads.length === 0 && actionError === null) closeBar();
			// Shift+Enter on a pair: the same stems wait and vocal solo as a plain pick, once the pair has landed and
			// aligned. A reload or another load since (IPC, MIDI, AutoPlay) is a newer track and is never soloed,
			// the same guard the plain pick uses (Codex P2 BLOCKING, #6691).
			if (vocalsOnly && landed !== null && decks[deck].load_generation === landed && decks[deck].stable_id === row.stable_id) {
				const soloLoads = deckLoadDispatches(deck);
				const settled = await waitForStemsSettled(
					() => ({ stable_id: decks[deck].load_generation === landed ? decks[deck].stable_id : null, status: decks[deck].stems.status }),
					row.stable_id
				);
				if (settled !== 'stale' && decks[deck].load_generation === landed && deckLoadDispatches(deck) === soloLoads) await soloVocals(deck, settled);
			}
			return null;
		} catch (exc) {
			via = null;
			if (barSession === session) pairLoads = pairLoads.filter((p) => p.id !== pairId);
			let message = exc instanceof Error ? exc.message : String(exc);
			// Only this pick's own load keeps the fader at 0: any other load on the deck (a refused
			// pick, another track, or the same track loaded again) gets its fader back.
			// Only a landed track still on the deck counts: once a later load replaced it, the rollback is owed.
			const predecessorLanded =
				(landedPicks.get(deck) ?? 0) - (holdsLanded ? 1 : 0) > 0 && landedGeneration.get(deck) === decks[deck].load_generation;
			if ((landed === null || decks[deck].load_generation !== landed) && faderUntouched() && !predecessorLanded) {
				const back = await runCommand({ type: 'fader', deck, value: priorFader });
				if (!back.ok) message += `; its fader did not go back to ${priorFader} (${back.reason})`;
			}
			// A newer opening of the bar is not told about this one's failure (Codex r4277100155); the
			// caller always is.
			const failure = `Deck ${deck}: ${message}`;
			if (barSession === session) actionError = failure;
			return failure;
		} finally {
			// A landed pick keeps the deck's landed count while any overlapping pick is still in its
			// prelude: that successor's rollback must see the partner it would otherwise restore over.
			// The count clears only when the last pick of the group settles (Codex r4280152481).
			if (inPrelude) {
				const shared = preludeBaseline.get(deck);
				if (shared !== undefined && shared.picks > 1) preludeBaseline.set(deck, { ...shared, picks: shared.picks - 1 });
				else {
					preludeBaseline.delete(deck);
					landedPicks.delete(deck);
					landedGeneration.delete(deck);
				}
			}
		}
	}

	function onWindowKeyup(event: KeyboardEvent): void {
		if (event.key === quickMod) quickHeld = false;
	}

	/** Escape clears a typed query first and closes the bar only when it is empty, wherever focus is. */
	function clearOrClose(): void {
		if (ejectGuard !== null) {
			ejectGuard = null;
			return;
		}
		if (picker !== null) {
			if (picker.guard !== null) picker.guard = null;
			else picker = null;
			return;
		}
		// Esc stops a sounding cue preview before it clears or closes anything.
		if (cue.stop()) return;
		if (query !== '') {
			query = '';
			selected = 0;
			touched = false;
		} else closeBar();
	}

	function onInputKeydown(event: KeyboardEvent): void {
		// Every key the bar owns stops here, so the page's own shortcuts
		// (Tab = compatible filter, Space = play) never see it.
		const handled = (): void => {
			event.preventDefault();
			event.stopPropagation();
		};
		if (previewMode !== 'off' && picker !== null) {
			if (pickerKey(event)) handled();
			else event.stopPropagation();
		} else if (ejectGuard !== null) {
			// The play-interrupt rule: only Cmd/Ctrl+Enter confirms; plain Enter and Esc keep it playing.
			handled();
			if (event.key === 'Enter' && quickModifierHeld(event, quickMod)) void confirmEject();
			// Space cancels here too, as on the focused buttons (CMDK-14, Sol r4278366197).
			else if (event.key === 'Enter' || event.key === 'Escape' || event.key === ' ') ejectGuard = null;
		} else if (event.key === 'Escape') {
			handled();
			clearOrClose();
		} else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
			handled();
			selected = stepSelection(selected, flat.length, event.key === 'ArrowDown' ? 1 : -1);
			actionError = null;
			touched = true;
			document.getElementById(`cmdk2-row-${selected}`)?.scrollIntoView({ block: 'nearest' });
		} else if (
			(event.key === 'ArrowLeft' || event.key === 'ArrowRight') &&
			previewMode !== 'off' &&
			touched &&
			selectedTrack !== undefined &&
			!event.metaKey &&
			!event.ctrlKey &&
			!event.altKey
		) {
			// A highlighted result takes ← →; with none highlighted they move the caret.
			handled();
			void cue.jump(event.key === 'ArrowRight' ? 1 : -1, event.shiftKey);
		} else if (event.key === 'Enter') {
			handled();
			activate(selectedEntry, event.shiftKey);
		} else if (altShortcut(event)) {
			handled();
		} else if (aiOn && nextMode(mode, event.key, { shift: event.shiftKey, meta: event.metaKey, ctrl: event.ctrlKey, alt: event.altKey }) !== null) {
			// CMDK-20: plain Tab flips search and AI search; Shift+Tab still cycles focus.
			handled();
			setMode(mode === 'plain' ? 'ai' : 'plain');
		} else if (event.key === 'Tab') {
			handled();
			cycleFocus(event.shiftKey);
		} else {
			event.stopPropagation();
		}
	}

	/**
	 * Alt+letter toggles a filter and Alt+1-4 picks the target deck, from the
	 * input or from any focused icon or ring. True when the key was one of them.
	 */
	function altShortcut(event: KeyboardEvent): boolean {
		if (!event.altKey) return false;
		const filter = filterForKeyCode(event.code);
		if (filter !== null) {
			toggleFilter(filter);
			return true;
		}
		const digit = /^Digit([1-4])$/.exec(event.code);
		if (digit === null) return false;
		const id = Number(digit[1]) as DeckId;
		if (DECK_IDS.includes(id)) target = id;
		return true;
	}

	/**
	 * Tab and Shift+Tab move focus around the bar's own controls (the input,
	 * the filter icons, the deck rings) and wrap, so every control is reachable
	 * from the keyboard and focus never leaves the dialog for the page behind.
	 */
	function cycleFocus(back: boolean): void {
		if (wrap === null) return;
		const stops = [...wrap.querySelectorAll<HTMLElement>('input, button:not([disabled])')];
		const next = cycleFocusIndex(stops.indexOf(document.activeElement as HTMLElement), stops.length, back);
		if (next >= 0) stops[next].focus();
	}

	/** Keys pressed while a filter icon or deck ring has focus. */
	function onWrapKeydown(event: KeyboardEvent): void {
		if (event.target === input) return;
		// The page's own shortcuts never see a key pressed inside the bar.
		event.stopPropagation();
		// A click on a picker row moves focus off the input; the picker keys still work.
		if (previewMode !== 'off' && picker !== null && pickerKey(event)) {
			event.preventDefault();
		} else if (ejectGuard !== null && (event.key === 'Enter' || event.key === ' ')) {
			// The play-interrupt rule holds on the guard's own buttons too: a focused "Eject it"
			// is not confirmed by plain Enter or Space (no native click), only by Cmd/Ctrl+Enter.
			event.preventDefault();
			if (event.key === 'Enter' && quickModifierHeld(event, quickMod)) void confirmEject();
			else ejectGuard = null;
		} else if (altShortcut(event)) {
			event.preventDefault();
		} else if (aiOn && nextMode(mode, event.key, { shift: event.shiftKey, meta: event.metaKey, ctrl: event.ctrlKey, alt: event.altKey }) !== null) {
			// CMDK-20 from a focused icon or ring too: plain Tab flips the mode, and the input
			// takes focus so the DJ can type the words straight away.
			event.preventDefault();
			setMode(mode === 'plain' ? 'ai' : 'plain');
			input?.focus();
		} else if (event.key === 'Tab') {
			event.preventDefault();
			cycleFocus(event.shiftKey);
		} else if (event.key === 'Escape') {
			event.preventDefault();
			clearOrClose();
		}
	}

	async function confirmEject(): Promise<void> {
		const guard = ejectGuard;
		if (guard === null) return;
		const op = ++ejectOp;
		ejectGuard = null;
		// A confirm answers only the load it asked about: after a newer load it asks again about
		// what is there now, playing or not (an emptied deck has nothing left to eject). The eject
		// is looked up after the drain, since the deck can offer a different one by then (Sol).
		const commandsAt = ejectCommands;
		if (!guardHolds(guard, decks[guard.id], deckLoadDispatches(guard.id), deckCommands(guard.id)) || deckCommands(guard.id) !== commandsAt) {
			const session = barSession;
			const again = await settledGuard(guard.id, session);
			// A newer confirm started, another eject took a guard while this one waited, or the bar
			// reopened: that guard answers its own deck, so this continuation must not replace it (Sol P1, #6689).
			if (op !== ejectOp || ejectGuard !== null || barSession !== session) return;
			const ejectNow = commandActions.find((cmd) => cmd.command.type === 'eject' && cmd.command.deck === guard.id);
			if (again !== null && ejectNow !== undefined && decks[guard.id].stable_id !== null) {
				ejectGuard = again;
				ejectCommands = deckCommands(guard.id);
			}
			return;
		}
		const action = commandActions.find((cmd) => cmd.command.type === 'eject' && cmd.command.deck === guard.id);
		if (action !== undefined) await runAction(action, true);
		input?.focus();
	}

	const GROUP_NAME: Record<'recent' | 'actions' | 'settings' | 'commands', string> = {
		recent: 'Recent loads',
		actions: 'Commands and settings',
		settings: 'Settings',
		commands: 'Commands'
	};

	function fmtBpm(bpm: number | null): string {
		return bpm === null ? '' : bpm.toFixed(1);
	}

	const VIA_LABEL = { album: 'album', genre: 'genre', comments: 'comment', notes: 'notes' } as const;
</script>

<svelte:window
	onkeydowncapture={onWindowKeydown}
	onkeyupcapture={onWindowKeyup}
	onblur={() => {
		quickHeld = false;
	}}
/>

{#snippet hl(text: string)}
	{#each highlightParts(text, tokens) as part, i (i)}{#if part.hit}<mark>{part.text}</mark>{:else}{part.text}{/if}{/each}
{/snippet}

{#snippet qk(i: number)}
	{#if quickHeld && i < 9}<kbd class="qk" data-testid="command-bar-quick-number">{quickLabel}{i + 1}</kbd>{/if}
{/snippet}

{#snippet actionView(action: CmdkAction, i: number)}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<li
		id={`cmdk2-row-${i}`}
		role="option"
		aria-selected={i === selected}
		class="row act"
		class:selected={i === selected}
		data-testid="command-bar-action"
		data-action-id={action.id}
		data-kind={action.kind}
		onmousemove={() => {
			selected = i;
		}}
		onclick={() => void runAction(action, false)}
	>
		<span class="t">
			{@render qk(i)}{@render hl(action.label)}<small>{action.kind === 'setting' ? action.path : action.hint}</small>
		</span>
		{#if action.kind === 'setting'}
			<span class="ent" data-testid="command-bar-action-hint">{settingVerb(action.def, action.value, settingTokens)}</span>
			{#if action.def.control.kind === 'boolean'}
				<span
					class="tog"
					class:on={action.value === true}
					role="switch"
					aria-checked={action.value === true}
					aria-label={action.label}
					title={action.value === true ? 'On' : 'Off'}
					data-testid="command-bar-setting-value"
				></span>
			{:else if action.def.control.kind === 'enum'}
				<span class="val" data-testid="command-bar-setting-value">{settingValueLabel(action.def, action.value)}</span>
			{:else}
				<span class="chip">page</span>
			{/if}
		{:else}
			<span class="chip">cmd</span>
		{/if}
	</li>
{/snippet}

{#snippet rowView(row: CmdkRow, i: number, pair?: CmdkPairTag)}
	{@const vias = matchedVia(row, tokens)}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<li
		id={`cmdk2-row-${i}`}
		role="option"
		aria-selected={i === selected}
		class="row"
		class:selected={i === selected}
		class:missing={row.file_exists === false}
		class:paired={pair !== undefined}
		class:with-inline={previewMode === 'inline' && i === selected && previewRow?.stable_id === row.stable_id}
		data-testid="command-bar-row"
		data-stable-id={row.stable_id}
		data-availability={row.file_availability ?? null}
		data-pair={pair === undefined ? null : pair.aligned ? 'aligned' : 'loose'}
		onmousemove={() => {
			touched = previewMode !== 'off';
			selected = i;
		}}
		onclick={(event) => activate({ kind: 'track', row, pair }, event.shiftKey)}
	>
		{#if pair !== undefined}<PairEdge seed={i * 7.7} />{/if}
		<span class="t">
			{@render qk(i)}{@render hl(row.title ?? '(untitled)')}<small>{@render hl(row.artist ?? '')}</small>
			{#each vias as via (via.field)}
				<span class="via" title={`Matched on ${VIA_LABEL[via.field]}`}>{VIA_LABEL[via.field]}: {@render hl(via.value)}</span>
			{/each}
		</span>
		<span class="m">
			<span title="Tempo in beats per minute">{@render hl(fmtBpm(row.bpm))}</span>
			{#if row.key !== null}<span class="dot">·</span><span title="Key">{@render hl(row.key)}</span>{/if}
			{#if aiFit.has(row.stable_id)}
				<span class="chip fit" title="JEV's probability that this track fits your words" data-testid="command-bar-ai-fit">{fitLabel(aiFit.get(row.stable_id) ?? 0)}</span>
			{/if}
			{#if pair !== undefined}
				<span class="pairtag" title="Paired with the master deck's track"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 1 L10.5 5.2 L6.4 6.4 L5.2 10.5 Z M6.4 6.4 L2 1" /></svg>PAIR</span>
				<span
					class="chip"
					class:aligned={pair.aligned}
					class:loose={!pair.aligned}
					data-testid="command-bar-pair-chip"
					title={pair.aligned
						? 'A sync snapshot was captured for this pair: it loads at that offset against the master, with the fader at 0'
						: 'No sync snapshot for this pair: it loads like any track'}>{pair.aligned ? 'aligned' : 'loose'}</span
				>
			{/if}
		</span>
		{#if previewMode === 'inline' && i === selected && previewRow?.stable_id === row.stable_id}
			<span class="inl">{@render strip('inline')}</span>
		{/if}
	</li>
{/snippet}

{#snippet listView(list: PlaylistNode, i: number)}
	{@const playable = list.track_count - list.broken_count}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<li
		id={`cmdk2-row-${i}`}
		role="option"
		aria-selected={i === selected}
		class="row"
		class:selected={i === selected}
		data-testid="command-bar-playlist"
		data-playlist-id={list.playlist_id}
		onmousemove={() => (selected = i)}
		onclick={() => activate({ kind: 'list', list }, false)}
	>
		<span class="t">{@render hl(list.name)}</span>
		<span class="m" title={`${playable} non-broken tracks, ${list.broken_count} broken tracks`}>{playable} tracks</span>
	</li>
{/snippet}

{#snippet strip(variant: 'above' | 'inline')}
	{#if previewRow !== null}
		<CmdkPreviewStrip
			{variant}
			title={`${previewRow.title ?? '(untitled)'}${previewRow.artist ? ` · ${previewRow.artist}` : ''}`}
			assets={previewData}
			anlz={previewPaintAnlz}
			duration_s={previewDuration}
			design={waveDesign}
			playhead={cuePlayhead}
			anchor={cueAnchor}
			message={cueMessage}
			palette={wavePaletteKey}
			onpainted={onPreviewPainted}
		/>
	{/if}
{/snippet}

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="cmdk2-backdrop" onclick={closeBar} data-testid="command-bar-backdrop">
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="wrap" bind:this={wrap} onclick={(event) => event.stopPropagation()} onkeydown={onWrapKeydown}>
			<div class="rings"><CmdkDeckRings {target} onPick={pickDeck} /></div>
			{#if previewMode === 'above' && previewRow !== null}
				<div class="strip-slot">{@render strip('above')}</div>
			{/if}
			<div class="cmdk2" role="dialog" aria-modal="true" aria-label="Command bar" tabindex="-1" data-testid="command-bar">
				<div class="head">
					<span
						class="mode"
						class:ai={mode === 'ai'}
						data-testid="command-bar-mode"
						title={aiOn ? (mode === 'ai' ? 'AI search: JEV ranks tracks that mix into the master by your words (Tab for plain search)' : 'Plain search (Tab for AI search)') : undefined}
						>{mode === 'ai' ? 'AI' : 'Search'}</span
					>
					{#if mode === 'ai' && aiState.status === 'working'}<span class="sweep" aria-hidden="true" data-testid="command-bar-ai-sweep"></span>{/if}
					<input
						bind:this={input}
						bind:value={query}
						oninput={() => {
							selected = 0;
							touched = false;
							picker = null;
						}}
						onkeydown={onInputKeydown}
						class="q"
						type="text"
						role="combobox"
						aria-expanded="true"
						aria-controls="cmdk2-list"
						aria-activedescendant={flat.length > 0 ? `cmdk2-row-${selected}` : undefined}
						autocomplete="off"
						spellcheck="false"
						placeholder={mode === 'ai'
							? 'Describe what you want next, e.g. uplifting vocal house'
							: actionsOn
								? 'Find a track, setting or command'
								: 'Find a track or playlist'}
						data-testid="command-bar-input"
					/>
					<div class="filters"><CmdkFilterRow {states} onToggle={toggleFilter} /></div>
				</div>
				{#if picker !== null}
					<CmdkDeckPicker
						trackTitle={picker.row.title ?? '(untitled)'}
						{query}
						decks={pickerDecks}
						selected={pickerSel}
						guard={picker.guard?.id ?? null}
						modKey={quickLabel.trim()}
						onHover={(index) => {
							const deck = pickerDecks[index];
							if (picker !== null && deck !== undefined) picker.sel = deck.id;
						}}
						onPick={(index) => pickerLoad(index, false)}
						onConfirm={confirmGuard}
						onCancel={() => {
							if (picker !== null) picker.guard = null;
							input?.focus();
						}}
					/>
					{#if pickerNote !== null}<p class="picker-note" role="status">{pickerNote}</p>{/if}
				{:else}
				<ul class="list" id="cmdk2-list" role="listbox" data-testid="command-bar-results">
					{#if ejectGuard !== null}
						{@const held = decks[ejectGuard.id]}
						<!-- The copy reads the deck as it is now: a confirm asked again after a newer load
						     describes that load, playing, paused or gone (CMDK-14, Sol r4278366191). -->
						<li role="presentation"><div class="guard" role="alertdialog" aria-label={held.playing ? 'Eject a playing deck' : 'Eject a deck'} data-testid="command-bar-eject-guard">
							{#if held.stable_id === null}
								<span>Deck {ejectGuard.id} is empty now; there is nothing to eject.</span>
							{:else if held.playing}
								<span>Deck {ejectGuard.id} is playing{held.title ? ` "${held.title}"` : ''}. Eject it?</span>
							{:else}
								<span>Deck {ejectGuard.id} holds{held.title ? ` "${held.title}"` : ' a track'}, paused. Eject it?</span>
							{/if}
							{#if held.stable_id !== null}
								<button type="button" class="confirm" onclick={() => void confirmEject()} data-testid="command-bar-eject-confirm">Eject it · {quickLabel.trim()}Enter</button>
							{/if}
							<button
								type="button"
								onclick={() => {
									ejectGuard = null;
									input?.focus();
								}}>{held.playing ? 'Keep playing' : 'Cancel'} · Esc</button
							>
						</div></li>
					{/if}
					{#each layout.groups as group, g (group.id)}
						<li class="group" role="presentation">
							{#if group.id === 'playlist'}
								<span>In open playlist · "{playlistTitle}"</span>
								<span title="Tracks in the open playlist that match">{group.total}</span>
							{:else if group.id === 'collection'}
								<span>In collection</span>
								<span title="Tracks in the rest of the collection that match">{group.total}</span>
							{:else if group.id === 'ai'}
								<span data-testid="command-bar-ai-head">AI picks · "{aiWords}"</span>
								<span title="Tracks JEV ranked, from the shortlist of unplayed tracks that mix into the master">{group.entries.length} of {aiShortlist.length}</span>
							{:else if group.id === 'pairs'}
								<span data-testid="command-bar-pairs-head">Paired with deck {masterDeck} · {masterDeck === null ? '' : (decks[masterDeck].title ?? '(untitled)')}</span>
								<span title="Tracks paired with the master deck's track">{group.entries.length}</span>
							{:else}
								<span>{GROUP_NAME[group.id]}</span>
							{/if}
						</li>
						{#if group.id === 'playlist'}
							{#each group.entries as entry, i (playlistKeys[i])}{#if entry.kind === 'track'}{@render rowView(entry.row, groupStart[g] + i)}{/if}{/each}
							{#if lists.rows.length > 0}
								<li class="group" role="presentation">
									<span>Playlists</span>
									<span title="Library playlists whose name matches">{lists.total}</span>
								</li>
								{#each lists.rows as list, li (list.playlist_id)}
									{@render listView(list, groupStart[g] + group.entries.length + li)}
								{/each}
							{/if}
						{:else}
							{#each group.entries as entry, i (entry.kind === 'track' ? `t-${entry.row.stable_id}` : `a-${entry.action.id}`)}
								{#if entry.kind === 'track'}{@render rowView(entry.row, groupStart[g] + i, entry.pair)}{:else}{@render actionView(entry.action, groupStart[g] + i)}{/if}
							{/each}
						{/if}
					{/each}
					{#if mode === 'ai'}
						{#if aiState.status === 'no-key'}
							<li
								class="hint ai-key"
								role="presentation"
								title="Set OPENROUTER_API_KEY in the engine's environment and restart the engine; there is no in-app key setting yet (PARITY-TODO: Cmd K AI search key)"
								data-testid="command-bar-ai-key"
							>Add an OpenRouter key for AI search</li>
						{:else if collectionError !== null}
							<li class="hint err" role="presentation" data-testid="command-bar-ai-status">AI search waits for the current library, which did not load: {collectionError}</li>
						{:else if collectionPending}
							<li class="hint" role="presentation" data-testid="command-bar-ai-status">Reading the current library before asking JEV...</li>
						{:else if aiState.status === 'error'}
							<li class="hint err" role="presentation" data-testid="command-bar-ai-status">AI search failed: {aiState.message}</li>
						{:else if aiShortlist.length === 0}
							<li class="hint" role="presentation" data-testid="command-bar-ai-status">No unplayed tracks mix into {masterDeck === null ? 'the library' : `deck ${masterDeck}`}</li>
						{:else if queryChars(aiWords) < AI_MIN_CHARS}
							<li class="hint" role="presentation" data-testid="command-bar-ai-status">
								Say what you want next ·
								<span title="Unplayed tracks within the master's tempo and one Camelot step of its key">{aiShortlist.length}</span>
								tracks mix into {masterDeck === null ? 'the library' : `deck ${masterDeck}`}
							</li>
						{:else if aiRanked.length === 0}
							<li class="hint" role="presentation" data-testid="command-bar-ai-status">
								Asking JEV about <span title="Tracks sent in one call">{aiShortlist.length}</span> tracks...
							</li>
						{/if}
					{:else}
					{#if pairsOn && tokens.length === 0 && masterDeck !== null && pairsState.status !== 'ready'}
						{#if pairsState.status === 'loading'}
							<li class="hint" role="presentation" data-testid="command-bar-pairs-status">Reading deck {masterDeck}'s pairs...</li>
						{:else if pairsState.status === 'error'}
							<li class="hint err" role="presentation" data-testid="command-bar-pairs-status">Pairs unavailable: {pairsState.message}</li>
						{/if}
					{/if}
					{#if layout.keepTyping}
						<li class="hint" role="presentation" data-testid="command-bar-keep-typing">Keep typing to search tracks</li>
					{/if}
					{/if}
					{#if groups.hiddenByFilters > 0}
						<li class="hint" role="presentation" data-testid="command-bar-hidden">
							<span title="Tracks that match the text but a library filter hides">{groups.hiddenByFilters}</span>
							hidden by filters
						</li>
					{/if}
					{#if awaitingCollection}
						<li class="hint" role="presentation" data-testid="command-bar-loading">
							{collectionError !== null ? 'Search is unavailable until the collection loads (close and reopen to retry)' : 'Loading the collection...'}
						</li>
					{:else if trackSearch && flat.length === 0 && tokens.length > 0 && groups.hiddenByFilters === 0}
						<li class="hint" role="presentation">No tracks or playlists match "{query}"</li>
					{/if}
				</ul>
				{/if}
				<div class="foot" data-testid="command-bar-status">
					{#if cmdkSearchTotalsLine !== null}
						<span
							data-testid="command-bar-list-totals"
							title="Track count and summed duration for matches in this search with library filters applied"
							>{cmdkSearchTotalsLine}</span
						>
					{/if}
					{#if collectionError !== null}
						<span class="err" role="alert">Collection search unavailable: {collectionError}</span>
					{:else if actionError !== null}
						<span class="err" role="alert" data-testid="command-bar-action-error">{actionError}</span>
					{:else if pairLoading !== null}
						<span data-testid="command-bar-pair-loading">Loading "{pairLoading.title}" to deck {pairLoading.deck} aligned to the master, fader at 0...</span>
					{:else if mode === 'ai' && ejectGuard === null && picker === null}
						<span><b>Tab</b> plain search</span>
						<span><b>↑↓</b> pick</span>
						<span><b>Enter</b> {previewMode !== 'off' ? 'pick a deck' : `load to deck ${target}`}</span>
						<span title="JEV sees each track's artist, title, album, genre, tempo and key, never its audio">ranked by JEV</span>
					{:else if ejectGuard !== null}
						<span><b>{quickLabel.trim()}Enter</b> eject the playing deck</span>
						<span><b>Esc</b> keep playing</span>
					{:else if picker !== null}
						<span><b>↑↓</b> or <b>W S</b> deck</span>
						<span><b>Enter</b> load</span>
						<span><b>←→</b> or <b>A D</b> move the cue</span>
						<span><b>Esc</b> back</span>
					{:else if selectedEntry?.kind === 'track' && selectedEntry.pair !== undefined && picker === null}
						<span><b>↑↓</b> pick</span>
						<span><b>Enter</b> {previewMode !== 'off' ? 'pick a deck' : `load to deck ${target}`}{selectedEntry.pair.aligned ? ', aligned to the master with the fader at 0' : ' (loose: like any track)'}</span>
						{#if previewMode !== 'off'}<span><b>←→</b> preview in the cue</span>{/if}
					{:else if actionsOn && selectedEntry?.kind === 'action'}
						<span><b>↑↓</b> pick</span>
						<span><b>Enter</b> {selectedEntry.action.kind === 'setting' ? 'change it' : 'run it'}</span>
						<span><b>hold {quickLabel.trim()}</b> quick pick</span>
					{:else if previewMode !== 'off'}
						<span><b>↑↓</b> pick</span>
						<span><b>←→</b> preview in the cue</span>
						<span><b>Enter</b> pick a deck</span>
						<span><b>⇧Enter</b> vocals only</span>
						<span><b>Alt 1-4</b> deck</span>
						{#if actionsOn}<span><b>hold {quickLabel.trim()}</b> quick pick</span>{/if}
						{#if firstPaintMs !== null}
							<span title="Milliseconds from asking for the highlighted track's overview to drawing it (budget 100 ms)" data-testid="command-bar-preview-paint-ms">{Math.round(firstPaintMs)} ms</span>
						{/if}
					{:else}
						<span><b>↑↓</b> pick</span>
						<span><b>Enter</b> {flat[selected]?.kind === 'list' ? 'open playlist' : `load to deck ${target}`}</span>
						<span><b>⇧Enter</b> vocals only</span>
						<span><b>Alt 1-4</b> deck</span>
						<span><b>Alt</b>+letter filter (hover an icon for its key)</span>
						{#if actionsOn}<span><b>hold {quickLabel.trim()}</b> quick pick</span>{/if}
					{/if}
				</div>
			</div>
		</div>
	</div>
{/if}

<style>
	.picker-note {
		margin: 4px 12px;
		font-size: 12px;
		opacity: 0.8;
	}
	.cmdk2-backdrop {
		/* Background dimming behind the bar (the maintainer, Thu 8 Oct 2026: "less dimming", 30%). */
		--cmdk-scrim: 30%;
		position: fixed;
		inset: 0;
		z-index: 900;
		background: rgb(0 0 0 / var(--cmdk-scrim));
		display: flex;
		justify-content: center;
		align-items: flex-start;
		padding-top: 18vh;
	}
	:global(html[data-theme='light']) .cmdk2-backdrop {
		background: rgb(255 255 255 / var(--cmdk-scrim));
	}
	.wrap {
		--rb-cmdk-pick: #a46ef0;
		position: relative;
		width: min(680px, calc(100vw - 32px));
	}
	:global(html[data-skin='mono-dev']) .wrap {
		--rb-cmdk-pick: #b58cff;
	}
	:global(html[data-theme='light']) .wrap {
		--rb-cmdk-pick: #7a3fd6;
	}
	.strip-slot {
		/* Above the deck rings, out of the flow, so the bar never moves when it appears. */
		position: absolute;
		left: 0;
		right: 0;
		bottom: calc(100% + 34px);
	}
	.inl {
		position: absolute;
		left: 8px;
		right: 8px;
		bottom: 1px;
		pointer-events: none;
	}
	.row {
		position: relative;
	}
	.row.with-inline {
		/* The inline overview (32 px) plus its one-line cue status. */
		padding-bottom: 52px;
	}
	.rings {
		position: absolute;
		right: 4px;
		bottom: calc(100% + 6px);
	}
	.cmdk2 {
		max-height: 62vh;
		display: flex;
		flex-direction: column;
		background: var(--rb-panel-raised, #1b1f26);
		border: 1px solid var(--rb-border, #2c323c);
		border-radius: 8px;
		box-shadow: 0 18px 50px rgb(0 0 0 / 45%);
		color: var(--rb-text, #e6e9ee);
		font-family: var(--rb-font, system-ui, sans-serif);
		overflow: hidden;
	}
	.head {
		display: grid;
		grid-template-columns: auto 1fr;
		align-items: center;
		gap: 4px 8px;
		padding: 10px 12px 6px;
		border-bottom: 1px solid var(--rb-border, #2c323c);
	}
	.mode {
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		padding: 2px 6px;
		border-radius: 3px;
		border: 1px solid var(--rb-border, #2c323c);
		color: var(--rb-text-dim, #8a94a3);
	}
	.mode.ai {
		color: var(--rb-pair, #a46ef0);
		border-color: var(--rb-pair, #a46ef0);
	}
	/* CMDK-21: a thin sweep along the top edge while JEV answers. */
	.head {
		position: relative;
	}
	.sweep {
		position: absolute;
		top: 0;
		left: 0;
		width: 30%;
		height: 2px;
		background: var(--rb-pair, #a46ef0);
		animation: cmdk-ai-sweep 1.1s ease-in-out infinite alternate;
	}
	@keyframes cmdk-ai-sweep {
		from {
			transform: translateX(0);
		}
		to {
			transform: translateX(233%);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.sweep {
			animation: none;
			width: 100%;
			opacity: 0.6;
		}
	}
	.chip.fit {
		margin-left: 6px;
		color: var(--rb-pair, #a46ef0);
		border-color: var(--rb-pair, #a46ef0);
	}
	.hint.ai-key {
		color: var(--rb-pair, #a46ef0);
	}
	.q {
		min-width: 0;
		background: transparent;
		border: 0;
		outline: none;
		color: inherit;
		font: 16px var(--rb-font, system-ui, sans-serif);
		padding: 2px 0;
	}
	.q::placeholder {
		color: var(--rb-text-dim, #8a94a3);
	}
	.filters {
		grid-column: 2;
	}
	.list {
		list-style: none;
		margin: 0;
		padding: 4px 8px;
		overflow-y: auto;
		flex: 1;
	}
	.group {
		display: flex;
		justify-content: space-between;
		gap: 8px;
		padding: 8px 4px 3px;
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: var(--rb-text-dim, #8a94a3);
	}
	.row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		padding: 6px 8px;
		border-radius: 5px;
		cursor: pointer;
	}
	.row.selected {
		background: color-mix(in srgb, var(--rb-cmdk-pick) 15%, var(--rb-panel-raised, #1b1f26));
	}
	.row.missing {
		opacity: 0.5;
	}
	.t {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 14px;
	}
	.t small {
		margin-left: 6px;
		font-size: 12px;
		color: var(--rb-text-dim, #8a94a3);
	}
	.t mark,
	.m mark {
		background: transparent;
		color: inherit;
		text-decoration: underline 2px var(--rb-cmdk-pick);
		text-underline-offset: 3px;
	}
	.via {
		margin-left: 8px;
		font-size: 10px;
		padding: 1px 5px;
		border-radius: 3px;
		border: 1px solid var(--rb-border, #2c323c);
		color: var(--rb-text-dim, #8a94a3);
	}
	.m {
		flex: none;
		font-size: 12px;
		color: var(--rb-text-dim, #8a94a3);
		font-variant-numeric: tabular-nums;
	}
	.dot {
		margin: 0 4px;
	}
	.hint {
		padding: 8px;
		font-size: 12px;
		color: var(--rb-text-dim, #8a94a3);
	}
	.foot {
		display: flex;
		flex-wrap: wrap;
		gap: 3px 12px;
		padding: 6px 12px;
		border-top: 1px solid var(--rb-border, #2c323c);
		font-size: 10px;
		color: var(--rb-text-dim, #8a94a3);
		opacity: 0.8;
	}
	.qk {
		font: 600 10px var(--rb-font-mono, ui-monospace, monospace);
		color: var(--rb-text, #e6e9ee);
		background: var(--rb-panel, #14171c);
		border: 1px solid var(--rb-border, #2c323c);
		border-bottom-width: 2px;
		border-radius: 3px;
		padding: 0 4px;
		margin-right: 8px;
		vertical-align: 1px;
	}
	.act .ent {
		/* "Enter to toggle" shows only on the row under the pointer or the highlight (the maintainer, Thu 8 Oct 2026). */
		visibility: hidden;
		margin-left: auto;
		font-size: 11px;
		color: var(--rb-text-dim, #8a94a3);
		white-space: nowrap;
	}
	.act.selected .ent,
	.act:hover .ent {
		visibility: visible;
	}
	.tog {
		width: 30px;
		height: 16px;
		border-radius: 8px;
		background: var(--rb-border, #2c323c);
		position: relative;
		flex: none;
	}
	.tog::after {
		content: '';
		position: absolute;
		top: 2px;
		left: 2px;
		width: 12px;
		height: 12px;
		border-radius: 50%;
		background: var(--rb-text-dim, #8a94a3);
		transition: transform 120ms;
	}
	.tog.on {
		background: var(--rb-cmdk-pick);
	}
	.tog.on::after {
		transform: translateX(14px);
		background: var(--rb-panel-raised, #1b1f26);
	}
	.val {
		flex: none;
		font-size: 12px;
		color: var(--rb-text, #e6e9ee);
	}
	.chip {
		flex: none;
		font: 600 10px var(--rb-font-mono, ui-monospace, monospace);
		letter-spacing: 0.05em;
		text-transform: uppercase;
		padding: 1px 5px;
		border-radius: 3px;
		border: 1px solid var(--rb-border, #2c323c);
		color: var(--rb-text-dim, #8a94a3);
	}
	/* CMDK-18: a pair row, its PAIR tag and aligned or loose chip (prototype "Pairs in Cmd K"). */
	.row.paired {
		margin-block: 2px;
	}
	.pairtag {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		margin-left: 8px;
		color: var(--rb-pair, #a46ef0);
		font: 700 10px var(--rb-font-mono, ui-monospace, monospace);
		letter-spacing: 0.06em;
	}
	.pairtag svg {
		width: 11px;
		height: 11px;
	}
	.pairtag svg path {
		fill: none;
		stroke: currentColor;
		stroke-width: 1.6;
		stroke-linejoin: round;
	}
	.chip.aligned,
	.chip.loose {
		margin-left: 6px;
		color: var(--rb-pair, #a46ef0);
		border-color: var(--rb-pair, #a46ef0);
	}
	.chip.loose {
		border-style: dashed;
	}
	.hint.err {
		color: var(--rb-red, #ff6b6b);
	}
	.guard {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		margin: 6px 0;
		padding: 8px;
		border: 1px solid var(--rb-red, #ff6b6b);
		border-radius: 5px;
		font-size: 12px;
	}
	.guard button {
		font: inherit;
		color: inherit;
		background: transparent;
		border: 1px solid var(--rb-border, #2c323c);
		border-radius: 4px;
		padding: 2px 8px;
		cursor: pointer;
	}
	.guard .confirm {
		border-color: var(--rb-red, #ff6b6b);
		color: var(--rb-red, #ff6b6b);
	}
	.foot .err {
		color: var(--rb-red, #ff6b6b);
	}

	.q:focus-visible {
		/* A11Y-11403: keyboard focus ring paired with this file's outline reset. */
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 2px;
	}
</style>
