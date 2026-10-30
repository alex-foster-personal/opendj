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
	import { tick, untrack } from 'svelte';
	import { deckStates as decks, DECK_IDS } from '$lib/rb/audio-engine.svelte';
	import {
		searchGroups,
		rowKeys,
		warmHaystacks,
		matchedVia,
		queryTokens,
		fillSearchFields,
		highlightParts,
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
	import { createPickLoader, deckLoadMark, loadCommitted, type SettleMeta } from '$lib/rb/cmdk-load';
	import { compatibleKeyChecked, computeNextOnlyRef } from '$lib/rb/next-only-filter';
	import CmdkDeckRings from './cmdk/CmdkDeckRings.svelte';
	import CmdkFilterRow from './cmdk/CmdkFilterRow.svelte';
	import CmdkPreviewStrip from './cmdk/CmdkPreviewStrip.svelte';
	import CmdkDeckPicker from './cmdk/CmdkDeckPicker.svelte';
	import { cmdkPreviewMode } from '$lib/rb/cmdk-flag';
	import { previewAssets, requestPreviewAssets, watchPreview } from '$lib/rb/cmdk-preview-data.svelte';
	import { createCuePreview, type CuePreviewCommand, type CuePreviewTrack } from '$lib/rb/cmdk-cue-preview.svelte';
	import { barPhase, downbeatTimes, snapStops } from '$lib/rb/cmdk-preview-grid';
	import { guardHolds, pickerAction, pickerArrow, pickerOrder, pickerStart, pickerStep, replaceGuard, type ReplaceGuard } from '$lib/rb/cmdk-deck-picker';
	import { previewCue } from '$lib/player/preview-cue.svelte';
	import type { AnlzBeat, AnlzData } from '$lib/rb/anlz-types';
	import { effectiveWaveformDesign, type UiSkin } from '$lib/rb/ui-skin';
	import type { WaveformDesignPref } from '$lib/rb/waveform-design';
	import { ensureAnlzPrefetch, getAnlzEntry } from './wave/anlz-cache.svelte';
	import { sectionSpans } from './wave/sections';

	/** What a row needs to be searched, filtered and loaded. */
	type CmdkRow = SearchableRow &
		Omit<FilterFields, 'file_availability'> & {
			file_exists: boolean | null;
			is_streaming: boolean | null;
			file_availability?: FileAvailabilityStatus;
			duration_ms?: number | null;
		};

	let {
		rows,
		playlistTitle,
		hideBroken,
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
		deckPending
	}: {
		rows: readonly CmdkRow[];
		playlistTitle: string;
		/** The broken filter as the open pane resolved it (Missing Tracks is exempt). */
		hideBroken: boolean;
		/** The browser's load path for a settled row (a refusal returns normally). */
		load: (row: CmdkRow, deck: DeckId) => Promise<void>;
		/** The table's own retrying rb-meta fetch, to settle a pending pick. */
		fetchMeta: (stable_id: string) => Promise<SettleMeta>;
		/** The browser's reservations on a deck, which move before its load commits. */
		reservation: (deck: DeckId) => number;
		soloVocals: (deck: DeckId, settled: 'ready' | 'unavailable' | 'error' | 'timeout') => Promise<void>;
		/** The page passes the prefs store and its filter setters, so this bar
		 * does not import prefs.svelte itself (keeps its fan-in down). */
		uiPrefs: CmdkFilterPrefs & { waveform_design: WaveformDesignPref; ui_skin: UiSkin; theme: string; wave_palette: string };
		setAvailableOfflineFilter: (next: boolean) => void;
		setHideBrokenLinks: (next: boolean) => void;
		setNextOnlyFilter: (next: boolean) => void;
		setRemixesFilter: (next: boolean) => void;
		setVocalsFilter: (next: boolean) => void;
		/** The typed dispatcher's UI entry, passed in by the page so this bar does
		 * not import performance-ipc.svelte itself (the most-imported module). */
		runCommand: (command: CuePreviewCommand) => Promise<{ ok: boolean }>;
		/** Whether the deck has a command still queued: the typed dispatcher's count, passed in as `runCommand` is. */
		deckPending: (deck: DeckId) => boolean;
	} = $props();

	/** Rows each group draws at once; the header count says when there are more. */
	const GROUP_LIMIT = 50;

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
	const ref = $derived(
		computeNextOnlyRef(
			DECK_IDS.map((d) => ({
				is_master: decks[d].is_master,
				playing: decks[d].playing,
				stable_id: decks[d].stable_id,
				key: decks[d].key,
				bpm: decks[d].bpm,
				key_shift_semitones: decks[d].key_shift_semitones
			})),
			compatibleKeyChecked(uiPrefs.compatible_filter)
		)
	);
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
	const awaitingCollection = $derived(
		(collectionPending || collectionError !== null || !searchWarm) && tokens.length > 0
	);
	/**
	 * The open pane's broken-filter exemption (Missing Tracks) covers its own
	 * rows only; the collection always follows the persisted preference.
	 */
	const keep = $derived({ playlist: cmdkKeep(uiPrefs, ref, hideBroken), collection: cmdkKeep(uiPrefs, ref) });
	const groups = $derived(
		awaitingCollection
			? { playlist: { rows: [], total: 0 }, collection: { rows: [], total: 0 }, hiddenByFilters: 0 }
			: searchGroups(playlistRows, collection, query, keep, GROUP_LIMIT)
	);
	/** A playlist can hold one track twice (LIBM-03), so its rows are keyed by membership. */
	const playlistKeys = $derived(rowKeys(groups.playlist.rows));
	/** Every selectable row, playlist group first, in the order drawn. */
	const flat = $derived([...groups.playlist.rows, ...groups.collection.rows]);

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
	let picker = $state<{ row: CmdkRow; vocalsOnly: boolean; sel: DeckId; guard: ReplaceGuard | null } | null>(null);
	/** When the dwell asked for the overview, for the first-paint readout. */
	let dwellAt = 0;
	/** The row the dwell asked for; a paint counts toward its first-paint time only while that row is shown. */
	let dwellId: string | null = null;
	let firstPaintMs = $state<number | null>(null);

	/** The track the strip and the cue preview are about: the picker's, else the highlighted row. */
	const previewRow = $derived(
		previewMode === 'off' ? null : (picker?.row ?? (touched ? (flat[selected] ?? null) : null))
	);
	const previewId = $derived(previewRow?.stable_id ?? null);
	const previewAnlz = $derived.by((): AnlzData | null => {
		if (previewId === null) return null;
		const entry = getAnlzEntry(previewId);
		return entry !== undefined && entry.status === 'ready' ? entry.data : null;
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
		const downbeats = previewAnlz === null ? [] : downbeatsOf(previewAnlz);
		const vocals =
			previewData?.status === 'ready' && (previewData.vocals.status === 'rekordbox' || previewData.vocals.status === 'demucs')
				? previewData.vocals.regions
				: [];
		return {
			stable_id: previewRow.stable_id,
			downbeats,
			stops: previewAnlz === null ? [] : snapStops(downbeats, sectionSpans(previewAnlz.phrases), vocals),
			duration_s: previewDuration,
			bpm: previewRow.bpm,
			// Not read yet (the dwell has not fired) or still loading: a press is held until it answers.
			gridPending: previewAnlz === null && getAnlzEntry(previewRow.stable_id)?.status !== 'error',
			untrusted: previewAnlz?.beatgrid.bar_phase_untrusted === true
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

	/** The row the dwell last fired for; null from the moment the highlight moves. */
	let dwelled = $state<string | null>(null);
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

	function openPicker(row: CmdkRow, vocalsOnly: boolean): void {
		picker = { row, vocalsOnly, sel: pickerDecks[pickerStart(pickerDecks, target)]?.id ?? target, guard: null };
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
			p.guard = replaceGuard(deck.id, decks[deck.id]);
			return;
		}
		target = deck.id;
		picker = null;
		void choose(p.row, p.vocalsOnly);
	}

	function confirmGuard(): void {
		const p = picker;
		const guard = p?.guard ?? null;
		if (p === null || guard === null) return;
		const index = pickerDecks.findIndex((deck) => deck.id === guard.id);
		p.guard = null;
		if (index < 0) return;
		// The deck took another load while the guard was open: the confirm answered a
		// question about a track that is gone, so ask again about what is there now, playing
		// or not. Only a confirm of the load it asked about goes ahead.
		if (!guardHolds(guard, decks[guard.id])) {
			p.guard = replaceGuard(guard.id, decks[guard.id]);
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
			if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) confirmGuard();
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
			query = query.slice(0, -1);
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

	async function openBar(): Promise<void> {
		query = '';
		selected = 0;
		touched = false;
		picker = null;
		target = defaultTargetDeck(deckList());
		open = true;
		loadCollection();
		await tick();
		input?.focus();
	}

	function closeBar(): void {
		open = false;
		picker = null;
		touched = false;
		cue.stop();
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
			else void openBar();
			return;
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
		(row, deck) => loadCommitted(() => decks[deck], row.stable_id, () => load(row, deck)),
		(deck) => deckLoadMark(decks[deck], reservation(deck)),
		(deck) => deckPending(deck)
	);

	async function choose(row: CmdkRow | undefined, vocalsOnly: boolean): Promise<void> {
		if (row === undefined) return;
		const deck = target;
		closeBar();
		// A pending pick's disk truth is settled first, and the pick is dropped if the
		// deck moved on meanwhile; a dropped or refused pick never solos vocals.
		const dispatched = await pickLoad(row, deck);
		if (!dispatched || !vocalsOnly || decks[deck].stable_id !== row.stable_id) return;
		// The solo is for THIS load: a reload of the same track (IPC, MIDI, AutoPlay) during the
		// wait moves the engine's load generation and reads as another track, so it is never soloed.
		const landed = decks[deck].load_generation;
		const settled = await waitForStemsSettled(
			() => ({ stable_id: decks[deck].load_generation === landed ? decks[deck].stable_id : null, status: decks[deck].stems.status }),
			row.stable_id
		);
		if (settled !== 'stale' && decks[deck].load_generation === landed) await soloVocals(deck, settled);
	}

	/** Escape clears a typed query first and closes the bar only when it is empty, wherever focus is. */
	function clearOrClose(): void {
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
		} else if (event.key === 'Escape') {
			handled();
			clearOrClose();
		} else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
			handled();
			selected = stepSelection(selected, flat.length, event.key === 'ArrowDown' ? 1 : -1);
			touched = true;
			document.getElementById(`cmdk2-row-${selected}`)?.scrollIntoView({ block: 'nearest' });
		} else if (
			(event.key === 'ArrowLeft' || event.key === 'ArrowRight') &&
			previewMode !== 'off' &&
			touched &&
			flat[selected] !== undefined &&
			!event.metaKey &&
			!event.ctrlKey &&
			!event.altKey
		) {
			// A highlighted result takes ← →; with none highlighted they move the caret.
			handled();
			void cue.jump(event.key === 'ArrowRight' ? 1 : -1, event.shiftKey);
		} else if (event.key === 'Enter') {
			handled();
			const row = flat[selected];
			if (previewMode !== 'off' && row !== undefined) openPicker(row, event.shiftKey);
			else void choose(row, event.shiftKey);
		} else if (altShortcut(event)) {
			handled();
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
		} else if (altShortcut(event)) {
			event.preventDefault();
		} else if (event.key === 'Tab') {
			event.preventDefault();
			cycleFocus(event.shiftKey);
		} else if (event.key === 'Escape') {
			event.preventDefault();
			clearOrClose();
		}
	}

	function fmtBpm(bpm: number | null): string {
		return bpm === null ? '' : bpm.toFixed(1);
	}

	const VIA_LABEL = { album: 'album', genre: 'genre', comments: 'comment', notes: 'notes' } as const;
</script>

<svelte:window onkeydowncapture={onWindowKeydown} />

{#snippet hl(text: string)}
	{#each highlightParts(text, tokens) as part, i (i)}{#if part.hit}<mark>{part.text}</mark>{:else}{part.text}{/if}{/each}
{/snippet}

{#snippet rowView(row: CmdkRow, i: number)}
	{@const vias = matchedVia(row, tokens)}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<li
		id={`cmdk2-row-${i}`}
		role="option"
		aria-selected={i === selected}
		class="row"
		class:selected={i === selected}
		class:missing={row.file_exists === false}
		class:with-inline={previewMode === 'inline' && i === selected && previewRow?.stable_id === row.stable_id}
		data-testid="command-bar-row"
		data-stable-id={row.stable_id}
		onmousemove={() => {
			touched = previewMode !== 'off';
			selected = i;
		}}
		onclick={(event) => {
			if (previewMode !== 'off') openPicker(row, event.shiftKey);
			else void choose(row, event.shiftKey);
		}}
	>
		<span class="t">
			{@render hl(row.title ?? '(untitled)')}<small>{@render hl(row.artist ?? '')}</small>
			{#each vias as via (via.field)}
				<span class="via" title={`Matched on ${VIA_LABEL[via.field]}`}>{VIA_LABEL[via.field]}: {@render hl(via.value)}</span>
			{/each}
		</span>
		<span class="m">
			<span title="Tempo in beats per minute">{@render hl(fmtBpm(row.bpm))}</span>
			{#if row.key !== null}<span class="dot">·</span><span title="Key">{@render hl(row.key)}</span>{/if}
		</span>
		{#if previewMode === 'inline' && i === selected && previewRow?.stable_id === row.stable_id}
			<span class="inl">{@render strip('inline')}</span>
		{/if}
	</li>
{/snippet}

{#snippet strip(variant: 'above' | 'inline')}
	{#if previewRow !== null}
		<CmdkPreviewStrip
			{variant}
			title={`${previewRow.title ?? '(untitled)'}${previewRow.artist ? ` · ${previewRow.artist}` : ''}`}
			assets={previewData}
			anlz={previewAnlz}
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
					<span class="mode">Search</span>
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
						placeholder="Find a track"
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
					{#if groups.playlist.rows.length > 0}
						<li class="group" role="presentation">
							<span>In open playlist · "{playlistTitle}"</span>
							<span title="Tracks in the open playlist that match">{groups.playlist.total}</span>
						</li>
						{#each groups.playlist.rows as row, i (playlistKeys[i])}{@render rowView(row, i)}{/each}
					{/if}
					{#if groups.collection.rows.length > 0}
						<li class="group" role="presentation">
							<span>In collection</span>
							<span title="Tracks in the rest of the collection that match">{groups.collection.total}</span>
						</li>
						{#each groups.collection.rows as row, i (row.stable_id)}{@render rowView(row, groups.playlist.rows.length + i)}{/each}
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
					{:else if flat.length === 0 && tokens.length > 0 && groups.hiddenByFilters === 0}
						<li class="hint" role="presentation">No tracks match "{query}"</li>
					{/if}
				</ul>
				{/if}
				<div class="foot" data-testid="command-bar-status">
					{#if collectionError !== null}
						<span class="err" role="alert">Collection search unavailable: {collectionError}</span>
					{:else if picker !== null}
						<span><b>↑↓</b> or <b>W S</b> deck</span>
						<span><b>Enter</b> load</span>
						<span><b>←→</b> or <b>A D</b> move the cue</span>
						<span><b>Esc</b> back</span>
					{:else if previewMode !== 'off'}
						<span><b>↑↓</b> pick</span>
						<span><b>←→</b> preview in the cue</span>
						<span><b>Enter</b> pick a deck</span>
						<span><b>⇧Enter</b> vocals only</span>
						<span><b>Alt 1-4</b> deck</span>
						{#if firstPaintMs !== null}
							<span title="Milliseconds from asking for the highlighted track's overview to drawing it (budget 100 ms)" data-testid="command-bar-preview-paint-ms">{Math.round(firstPaintMs)} ms</span>
						{/if}
					{:else}
						<span><b>↑↓</b> pick</span>
						<span><b>Enter</b> load to deck {target}</span>
						<span><b>⇧Enter</b> vocals only</span>
						<span><b>Alt 1-4</b> deck</span>
						<span><b>Alt</b>+letter filter (hover an icon for its key)</span>
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
	.foot .err {
		color: var(--rb-red, #ff6b6b);
	}
</style>
