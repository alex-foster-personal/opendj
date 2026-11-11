<script lang="ts">
	// Build unit: deck (COMPONENT-MAP 1.3, SCREENSHOT-SPEC 3).
	// REAL: header meta + artwork, strip overview waveform click-to-seek,
	// hot-cue bank jumps, INT beat-loop cluster, CUE + play/pause transport,
	// Q, BEAT SYNC, MASTER, MT, KEY SYNC, key nudge, SLIP, jog readouts +
	// position tick, pitch fader + 8/16/WIDE range switcher. Stems are live
	// only for validated real Demucs artifacts.
	// Grid adjust / grid shift (GRIDEDIT-6235) live in deck/GridEditButtons.
	// INERT (plannedTitle per control): HOT CUE dropdown, AU, MA.
	//
	// ALL live state comes from the audio-engine accessor: the engine unit
	// owns DeckState (types.ts) via the rune module audio-engine.svelte.ts.
	import { onWheelFaderAdjust } from '$lib/rb/fader-ghost.svelte';
	import { clickSelect, getSelectedDecks, isSelected } from '$lib/rb/mixer-selection.svelte';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import {
	DECK_IDS,
	deckStates,
	getDeckState,
		engine,
		mixerState,
		parseCamelotKey,
		pitchRanges
	} from '$lib/rb/audio-engine.svelte';
	import { fetchMemoryCues, setActiveMemoryLoop as putActiveMemoryLoop } from '$lib/rb/api-rb';
	import type { PitchRange } from '$lib/rb/audio-engine.svelte';
	import { bannerSyncError, deckErrorIds, noteDeckError } from '$lib/rb/deck-error-id.svelte';
	import { deckBannerDismissMs, deckBannerTone } from '$lib/rb/deck-banner-policy';
	import { presentationTickFailure } from '$lib/rb/presentation-tick-failure.svelte';
	import { createDeckHotCueActions } from '$lib/rb/deck-hot-cue-actions';
	import {
		performanceCommandStatus,
		dismissPerformanceDeckError,
		dispatchPerformanceCommand,
		queryPerformanceState,
		queryQuantizedLaunchArmed,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import { masterButtonCommand } from '$lib/rb/master-election';
	import { pushToast } from '$lib/stores.svelte';
	import {
		deckHoverEnter,
		deckHoverLeave,
		deckHoverLinkId,
		deckIdFromHoverEl
	} from '$lib/rb/deck-hover.svelte';
	import { formatLoadLatency } from '$lib/rb/format-load-latency';
	import { lyricEntry, loadLyrics } from '$lib/lyrics/lyrics-cache.svelte';
	import { adaptLyricTrack } from '$lib/rb/lyrics/build-track';
	import type { LyricsTrack } from '$lib/rb/lyrics/types';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import {
		acceptTrackDragOver,
		applyDeckTrackDrop,
		droppedRowFlags,
		endTrackDrag,
		primaryDroppedStableId
	} from '$lib/rb/track-drag.svelte';
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { DeckState, QuantizeGrid } from '$lib/rb/deck-state-types';
	import type { HotCueSlot } from '$lib/rb/hot-cue-types';
	import { quickModePlan, type StemQuickMode } from '$lib/rb/stem-quick-mode';
	import type { StemControl } from '$lib/rb/stem-types';
	import BeatJump from './deck/BeatJump.svelte';
	import type { BeatJumpSize } from '$lib/rb/beat-jump-size.svelte';
	import DeckHeader from './deck/DeckHeader.svelte';
	import HotCueBank from './deck/HotCueBank.svelte';
	import JogDial from './deck/JogDial.svelte';
	import LoopCluster from './deck/LoopCluster.svelte';
	import PadStrip from './deck/PadStrip.svelte';
	import PitchFader from './deck/PitchFader.svelte';
	import DeckErrorBanner from './deck/DeckErrorBanner.svelte';
	import DeckLyricLine from './deck/DeckLyricLine.svelte';
	import { DECK_PAUSE_DIM_AFTER_MS, deckActivityLook } from './deck/deck-activity-look';
	import { deckLyricRowBudget, deckLyricStripPx } from '$lib/rb/lyrics/deck-lyric-row-budget';
	import PadModeMenu from './deck/PadModeMenu.svelte';
	import SecondaryLoadBadge from './deck/SecondaryLoadBadge.svelte';
	import StemRow from './deck/StemRow.svelte';
	import { toggleTrackLoadResetPanel, trackLoadResetEnabled } from '$lib/rb/track-load-reset.svelte';
	import {
		loadedStemSeparation,
		stemSeparation,
		stemSeparationLoadError,
		type StemSeparationPort
	} from '$lib/rb/stem-separation-port';
	import { stemStatusView } from '$lib/rb/stem-status';
	import { subscribeLoadedDeckStemsRefresh } from '$lib/rb/loaded-deck-stems-refresh';
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import { rustCommandUnsupported } from '$lib/audio-engine/rust-mode.svelte';
	import { SeparateEndProbe, lookForBundle, separateRunId } from '$lib/rb/stem-separate-probe';
	import StripWaveform from './deck/StripWaveform.svelte';
	import { waveSeekCommand } from './wave/wave-scrub';
	import TransportCluster from './deck/TransportCluster.svelte';
	import { selId, selectorNode } from '$lib/keyboard/selector-register';
	import GridEditButtons from './deck/GridEditButtons.svelte';
	import ReactIsland from '$lib/react/ReactIsland.svelte';
	import { reactDeckEnabled } from '$lib/react/react-deck-flag';

	let { deckId }: { deckId: DeckId } = $props();

	const selected = $derived(isSelected(deckId));

	function handleDeckClick(event: MouseEvent): void {
		clickSelect(deckId, event.shiftKey);
	}

	function adjustSelectedFaders(delta: number): void {
		const decks = getSelectedDecks().includes(deckId) ? getSelectedDecks() : [deckId];
		for (const deck of decks) {
			const before = mixerState.channels[deck].fader;
			const next = Math.min(1, Math.max(0, before + delta));
			onWheelFaderAdjust(deck, before, next, false);
			void runPerformanceCommandFromUi({ type: 'fader', deck, value: next });
		}
	}

	function handleDeckWheelAdjust(next: number): void {
		const before = mixerState.channels[deckId].fader;
		const delta = next - before;
		if (delta === 0) return;
		adjustSelectedFaders(delta);
	}

	const deck: DeckState = $derived(getDeckState(deckId));
	const pitchRange: PitchRange = $derived(pitchRanges[deckId]);
	const pending: boolean = $derived(performanceCommandStatus.deck_pending[deckId] > 0);

	// DECKGAP-7691f (Preview pin aa2989e367d7): pause age is wall-clock, not playhead. The clock restarts on a new load
	// and when transport starts. A loaded deck that never started counts.
	let pausedSinceMs = $state<number | null>(null);
	let pauseAnchor = '';
	let pauseDimTick = $state(0);

	$effect(() => {
		const stableId = deck.stable_id;
		const generation = deck.load_generation;
		const playing = deck.playing;
		if (stableId === null || playing) {
			pauseAnchor = '';
			if (pausedSinceMs !== null) pausedSinceMs = null;
			return;
		}
		const anchor = `${stableId}:${generation}`;
		if (pauseAnchor === anchor && pausedSinceMs !== null) return;
		pauseAnchor = anchor;
		pausedSinceMs = Date.now();
	});

	$effect(() => {
		const since = pausedSinceMs;
		if (since === null) return;
		const wait = Math.max(0, DECK_PAUSE_DIM_AFTER_MS - (Date.now() - since));
		const timer = setTimeout(() => {
			pauseDimTick += 1;
		}, wait);
		return () => clearTimeout(timer);
	});

	const deckLook = $derived.by(() => {
		void pauseDimTick;
		return deckActivityLook({
			loaded: deck.stable_id !== null,
			playing: deck.playing,
			audible: deck.audible,
			transportPending: deck.transport_pending,
			commandPending: pending,
			positionMs: deck.position_ms,
			durationMs: deck.duration_ms,
			loopEngaged: deck.loop !== null && deck.loop.engaged,
			pausedForMs: pausedSinceMs === null ? null : Date.now() - pausedSinceMs
		});
	});
	const controlError: string | null = $derived(
		// DECKGAP-7691c: an unsynced start's reason glows on Play; it is not a deck error (PLAY-27).
		performanceCommandStatus.deck_errors[deckId] ??
			bannerSyncError(deck.sync_error) ??
			deck.processor_error ??
			presentationTickFailure()
	);

	/**
	 * Mint and log the banner's id whenever what the banner shows changes.
	 *
	 * An $effect rather than a $derived because this WRITES a log row, and a
	 * derivation that logs would fire on Svelte's own re-evaluation schedule
	 * rather than on real changes. `noteDeckError` is idempotent for an
	 * unchanged message, so the re-runs this effect takes from the three
	 * unrelated deck-state sources behind `controlError` cost nothing.
	 *
	 * Covers all three sources uniformly. That matters because only two of them
	 * (`_persistCommandError`, `_recordProcessorFailure`) raise a toast; the
	 * Beat Sync paths set `sync_error` directly and had no id anywhere at all.
	 */
	$effect(() => {
		noteDeckError(deckId, controlError);
	});

	const controlErrorId: string | null = $derived(deckErrorIds[deckId]);

	// DECKUX-LABEL-02: an expected, self-resolving condition (stems still
	// loading) clears itself and is not painted as a fault; a real fault stays
	// until dismissed. Primitives, so the timer restarts only on a real change.
	const controlErrorDismissMs: number | null = $derived(
		controlError === null ? null : deckBannerDismissMs(controlError, deck.stems.status)
	);
	$effect(() => {
		const ms = controlErrorDismissMs;
		if (ms === null) return;
		const timer = setTimeout(() => dismissPerformanceDeckError(deckId), ms);
		return () => clearTimeout(timer);
	});

	// REACT-01 (`ui.react.deck`, default OFF): the deck's leaf controls render
	// through their React twins only when the daemon declares the flag on.
	// BeatJump stays Svelte either way: it reads deck.position_ms, which the
	// rAF clock writes every frame, and per-frame values never go through React.
	const reactDeck: boolean = $derived(reactDeckEnabled());
	/** #9397: two-deck mode clips decks 3/4, so they leave the keyboard selector. */
	const deckSelectable = $derived(!(uiPrefs.deck_layout === 'less' && (deckId === 3 || deckId === 4)));
	// StemRow's twin reads only deck_id and stems. A plain snapshot of exactly
	// those keeps React re-renders to stem changes; passing the live deck would
	// re-render on nothing (React cannot see rune reads) or, spread, every frame.
	const stemRowDeck: Pick<DeckState, 'deck_id' | 'stems'> = $derived({
		deck_id: deck.deck_id,
		stems: $state.snapshot(deck.stems)
	});
	const keySyncAvailable: boolean = $derived(
		deck.stable_id !== null &&
		parseCamelotKey(deck.key) !== null &&
		DECK_IDS.some(
			(candidate) =>
				candidate !== deckId &&
				deckStates[candidate].is_master &&
				deckStates[candidate].stable_id !== null &&
				parseCamelotKey(deckStates[candidate].key) !== null
		)
	);

	const hotCueActions = createDeckHotCueActions(() => deckId, () => deck);

	$effect(() => {
		const stableId = deck.stable_id;
		if (!uiPrefs.lyrics_deck_line || !uiPrefs.lyrics_global || stableId === null) return;
		void loadLyrics(stableId);
	});

	const deckLyricEntry = $derived(deck.stable_id === null ? null : lyricEntry(deck.stable_id));
	const deckLyrics: { track: LyricsTrack | null; error: string | null } = $derived.by(() => {
		const stableId = deck.stable_id;
		const entry = deckLyricEntry;
		if (stableId === null || entry === null) return { track: null, error: null };
		if (entry.state === 'error') return { track: null, error: entry.error };
		if (entry.state !== 'loaded' || entry.track === null) return { track: null, error: null };
		try {
			return {
				track: adaptLyricTrack(
					{
						id: stableId,
						artist: deck.artist ?? '',
						title: deck.title ?? '',
						duration_s: (deck.duration_ms ?? 0) / 1000
					},
					entry.track
				),
				error: null
			};
		} catch (error) {
			return {
				track: null,
				error: error instanceof Error ? error.message : String(error)
			};
		}
	});

	function presentedPositionSec(): number | null {
		if (deck.stable_id === null) return null;
		return deck.position_ms / 1000;
	}

	let cueFlexEl: HTMLDivElement | null = $state(null);
	let cueFlexHeightPx = $state(0);
	let padBankHeightPx = $state(0);
	const deckLyricRows = $derived(deckLyricRowBudget(cueFlexHeightPx, padBankHeightPx));
	const deckLyricStripHeight = $derived(deckLyricStripPx(deckLyricRows));

	$effect(() => {
		const el = cueFlexEl;
		if (el === null || typeof ResizeObserver === 'undefined') return;
		const padBank = el.querySelector('.cue-area');
		const ro = new ResizeObserver((entries) => {
			for (const entry of entries) {
				if (entry.target === el) cueFlexHeightPx = entry.contentRect.height;
				else if (padBank !== null && entry.target === padBank) {
					padBankHeightPx = entry.contentRect.height;
				}
			}
		});
		ro.observe(el);
		if (padBank instanceof HTMLElement) ro.observe(padBank);
		return () => ro.disconnect();
	});

	// ------------------------------------------------- engine call plumbing
	// Engine methods throw loudly on empty decks (fail-fast contract);
	// callers below are gated by disabled states, never by silent catches.

	async function seekTo(ms: number): Promise<void> {
		// The strip already snapped the target (WAVE-SEEK-01): exact, no grid re-snap.
		await runPerformanceCommandFromUi(waveSeekCommand(deckId, ms));
	}

	/** #884: a populated hot-cue pad, unlike a plain waveform seek, may need to
	 * honour BeatSyncMax - routed through hot_cue_trigger, not seekTo, so it
	 * can arm for the deck's own next downbeat instead of jumping immediately.
	 * Q1: see `playPause` for the `pressT0Ms` contract. */
	async function triggerHotCue(slot: HotCueSlot, pressT0Ms?: number): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'hot_cue_trigger', deck: deckId, slot }, pressT0Ms);
	}

	/**
	 * Q1: `pressT0Ms` is the ORIGINATING click's `event.timeStamp`, threaded
	 * from the button rather than re-read here. `runPerformanceCommandFromUi`
	 * defaults it to its own `performance.now()`, which is already downstream of
	 * the browser's input queue and of handler dispatch - real time the operator
	 * waited, and previously invisible to `press_to_schedule_ms` on every one of
	 * these paths, because no call site in the app has ever passed the stamp.
	 */
	async function playPause(pressT0Ms?: number, quantize?: boolean): Promise<void> {
		const playing = !deck.playing;
		await runPerformanceCommandFromUi(
			{
				type: 'play',
				deck: deckId,
				playing,
				...(quantize === true && playing ? { quantize: true } : {})
			},
			pressT0Ms
		);
	}

	/** Q1: see `playPause` for the `pressT0Ms` contract. */
	async function returnToCue(pressT0Ms?: number): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'cue', deck: deckId }, pressT0Ms);
	}

	async function callMemoryCue(direction: 'prev' | 'next', pressT0Ms?: number): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'memory_cue_call', deck: deckId, direction }, pressT0Ms);
	}

	async function engageBeatLoop(beats: number, startMs?: number): Promise<void> {
		await runPerformanceCommandFromUi(
			startMs === undefined
				? { type: 'beat_loop', deck: deckId, beats }
				: { type: 'beat_loop', deck: deckId, beats, start_ms: startMs }
		);
	}

	/** DECKCTL-6235d: engage a stored hot-cue or memory loop exactly. */
	async function engageStoredLoop(loop: { in_ms: number; out_ms: number }): Promise<boolean> {
		return (await runPerformanceCommandFromUi({ type: 'loop', deck: deckId, loop, quantize: false })).ok;
	}

	/** CUES-9967: set or clear the track's active memory loop, then re-read
	 * the cues; setting it arms the safety slot now, clearing it disarms the
	 * slot when the slot holds that loop. */
	async function setActiveMemoryLoop(
		loop: { in_ms: number; out_ms: number },
		active: boolean
	): Promise<boolean> {
		const stableId = deck.stable_id;
		try {
			if (stableId === null) throw new Error('no track loaded');
			const { revision } = await fetchMemoryCues(stableId);
			await putActiveMemoryLoop(stableId, loop.in_ms, active, revision);
			await engine.refreshHotCues(deckId);
			if (active) {
				engine.armActiveLoopFromCues(deckId);
			} else if (deck.safety_loop?.in_ms === loop.in_ms && deck.safety_loop.out_ms === loop.out_ms) {
				await armSafetyLoop(false);
			}
			return true;
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`Active loop ${active ? 'set' : 'clear'} failed - ${message}`, 'error');
			return false;
		}
	}

	async function disengageLoop(): Promise<boolean> {
		return (await runPerformanceCommandFromUi({ type: 'loop', deck: deckId, loop: null })).ok;
	}

	async function loopIn(): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'loop_in', deck: deckId });
	}

	async function loopOut(): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'loop_out', deck: deckId });
	}

	async function reloop(): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'reloop', deck: deckId });
	}

	async function beatJump(beats: number): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'beat_jump', deck: deckId, beats });
	}

	async function setBeatJumpSize(beats: BeatJumpSize): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'beat_jump_size', deck: deckId, beats });
	}

	async function setLoopIntervalMode(enabled: boolean): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'loop_interval_mode', deck: deckId, enabled });
	}

	async function setLoopIntervalBase(base: number): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'loop_interval_base', deck: deckId, base });
	}

	async function saveSafetyLoop(): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'safety_loop_save', deck: deckId });
	}

	async function armSafetyLoop(armed: boolean): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'safety_loop_arm', deck: deckId, armed });
	}

	async function clearSafetyLoop(): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'safety_loop_clear', deck: deckId });
	}

	/** Q1-DEFAULT-ON: the Q button is a person's toggle, so its off sticks for this track. */
	async function toggleQuantize(): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'quantize',
			deck: deckId,
			enabled: !deck.quantize_enabled,
			by_user: true
		});
	}

	/** Pin a67bafbfc4b0: change the quantize GRID (1/4/8 beats, or
	 * DECKCTL-6235c 'phase': snap to the nearest phrase boundary). */
	async function setQuantizeGrid(beats: QuantizeGrid): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'quantize_grid', deck: deckId, beats });
	}

	async function toggleBeatSync(): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'beat_sync',
			deck: deckId,
			enabled: !deck.beat_sync_enabled
		});
	}

	/** Click picks this deck and keeps AUTO handoff; long-press or Shift/Alt-click locks. */
	async function selectMaster(lockGesture: boolean): Promise<void> {
		await runPerformanceCommandFromUi(masterButtonCommand(queryPerformanceState(), deckId, lockGesture));
	}

	async function toggleMasterTempo(): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'master_tempo',
			deck: deckId,
			enabled: !deck.master_tempo_enabled
		});
	}

	/** STEM-48: retry a failed stem load, or start one that is being held. */
	async function loadStems(): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'stem_load', deck: deckId });
	}

	// STEM-9934: a deck on NO STEMS looks for its bundle again when a stems job
	// (library_jobs) or a library change (tracks) names its track, and on resync,
	// clearing the cached no-stems flag first (loaded-deck-stems-refresh.ts).
	$effect(() => {
		const id = deckId;
		return subscribeLoadedDeckStemsRefresh({
			deck: id,
			state: () => getDeckState(id),
			subscribeKind,
			subscribeResync,
			canLoadStems: () => !rustCommandUnsupported('stem_load'),
			loadStems: (stable_id) => runPerformanceCommandFromUi({ type: 'stem_load', deck: id, stable_id })
		});
	});

	// STEM-77: a deck showing NO STEMS offers to separate the loaded track.
	const separateId = $derived(
		deck.stable_id !== null && stemStatusView(deck.stems).name === 'none' ? deck.stable_id : null
	);
	// The separation store (and the jobs client behind it) loads on demand, the
	// first time a deck shows NO STEMS, so it is not in this route's first load
	// (stem-separation-port.ts). Until it answers the button reads "Checking";
	// a chunk that fails to load is the button's refusal, with its reason.
	let separation = $state<StemSeparationPort | null>(loadedStemSeparation());
	let separationLoadError = $state<string | null>(null);
	$effect(() => {
		if (separateId === null || separation !== null || separationLoadError !== null) return;
		stemSeparation().then(
			(port) => {
				separation = port;
			},
			(exc: unknown) => {
				// The port's own text, so the button and the query say the same.
				separationLoadError = stemSeparationLoadError() ?? String(exc);
			}
		);
	});
	$effect(() => {
		if (separateId === null || separation === null) return;
		const store = separation;
		store.ensureChecked();
		// Date.now() is not reactive, so a stale "no source" answer is re-read on a timer.
		const timer = setInterval(() => store.ensureChecked(), 15_000);
		return () => clearInterval(timer);
	});
	// The button's words come from the store's chunk too (separateActionView);
	// until it is loaded the deck says only "checking" or the load failure,
	// in the same words (tests/unit/stem-separate.test.mjs keeps them equal).
	const separateView = $derived(
		separateId === null
			? null
			: separation === null
				? {
						label: 'SEPARATE',
						tip:
							separationLoadError === null
								? 'Checking whether this machine can separate stems...'
								: `Separation is not available here: ${separationLoadError}.`,
						disabled: true
					}
				: separation.actionView(
						separateId,
						separateProbed(separation, separateId),
						separateProbeFailed(separation, separateId)
					)
	);
	// However the covering job ended (a library job can fail after writing
	// this track's bundle), look for the bundle once before showing the
	// job's verdict: `stem_load` loads it if it is there.
	// Only a stem_load that RAN is an answer: one refused (a preset lock, no
	// session) leaves the probe failed and the button offers to look again.
	let separateProbeAnswers = $state(0);
	// Sol 4291048002: `stem_load` loads whatever the deck holds NOW, so it is an
	// answer for `stableId` only while the deck still holds that track.
	// The stem_load returns once the deck has started reloading, so the look
	// waits for that load to settle before it answers (Codex on #6819).
	let stemsSettledWaiters: Array<() => void> = [];
	function stemsSettled(): Promise<void> {
		if (deck.stems.status !== 'loading') return Promise.resolve();
		return new Promise((resolve) => stemsSettledWaiters.push(resolve));
	}
	$effect(() => {
		if (deck.stems.status === 'loading' || stemsSettledWaiters.length === 0) return;
		const waiters = stemsSettledWaiters;
		stemsSettledWaiters = [];
		for (const resolve of waiters) resolve();
	});
	const separateProbe = new SeparateEndProbe(
		async (stableId) =>
			separateId === stableId &&
			lookForBundle({
				run: async () => (await runPerformanceCommandFromUi({ type: 'stem_load', deck: deckId, stable_id: stableId })).ok,
				settled: stemsSettled,
				holds: () => deck.stable_id === stableId
			}),
		() => {
			separateProbeAnswers += 1;
		}
	);
	function separateProbed(store: StemSeparationPort, stableId: string): boolean {
		void separateProbeAnswers;
		const job = store.job(stableId);
		return job !== null && separateProbe.probed(stableId, separateRunId(job));
	}
	function separateProbeFailed(store: StemSeparationPort, stableId: string): boolean {
		void separateProbeAnswers;
		const job = store.job(stableId);
		return job !== null && separateProbe.failed(stableId, separateRunId(job));
	}
	$effect(() => {
		if (separateId === null || separation === null) return;
		separation.noteNoStems(separateId);
		void separateProbe.observe(separateId, separation.job(separateId));
	});

	async function separateStems(): Promise<void> {
		if (separateId === null) return;
		// CHECK STEMS: the look for the bundle after the job ended did not
		// run, so look again; nothing is known yet that would justify paying
		// for another separation.
		const job = separation?.job(separateId) ?? null;
		if (job !== null && separateProbe.failed(separateId, separateRunId(job))) {
			await separateProbe.retry(separateId, job);
			return;
		}
		// Otherwise a new separation: a job that ended was already followed by
		// a bundle probe that RAN (separateProbe), and the button only enables
		// once it answered with the deck still on NO STEMS, so another load
		// would find nothing again. The same typed command a browser agent
		// sends; the store behind it refuses a second job while one for this
		// track is still under way.
		await runPerformanceCommandFromUi({ type: 'stem_separate', deck: deckId, stable_id: separateId });
	}

	async function toggleStemMute(stem: StemControl): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'stem_mute',
			deck: deckId,
			stem,
			muted: !deck.stems.controls[stem].muted
		});
	}

	async function toggleStemSolo(stem: StemControl): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'stem_solo',
			deck: deckId,
			stem,
			solo: !deck.stems.controls[stem].solo
		});
	}

	/** STEMPERF-11695: one press to acapella, instrumental or everything, as the existing stem commands. */
	async function applyStemQuickMode(mode: StemQuickMode): Promise<void> {
		for (const command of quickModePlan(deckId, mode, deck.stems)) {
			await runPerformanceCommandFromUi(command);
		}
	}

	async function toggleSlip(): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'slip',
			deck: deckId,
			enabled: !deck.slip_enabled
		});
	}

	async function syncKey(): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'key_sync',
			deck: deckId,
			enabled: !deck.key_sync_enabled
		});
	}

	async function nudgeKey(semitones: number): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'key_nudge', deck: deckId, semitones });
	}

	// ------------------------------------------- hot-cue SAVE / CLEAR
	// Persistence writes (djmdCue Kind 1-8), not performance commands -
	// they go straight to the REST write surface, then refresh the deck's
	// hot_cues from the backend (rb_vendor.fetch_cues is always live).

	async function setTempo(ratio: number): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'tempo', deck: deckId, ratio });
	}

	// setPitchRange throws if the current pitch no longer fits the target
	// range (engine contract: explicit reset, never a silent clamp) - reset
	// to 0% first so the range switch always succeeds visibly.
	async function setPitchRangeUi(range: PitchRange): Promise<void> {
		if (range !== pitchRange && Math.abs(deck.pitch - 1) * 100 > range + 1e-9) {
			await runPerformanceCommandFromUi({ type: 'tempo', deck: deckId, ratio: 1 });
			if (Math.abs(getDeckState(deckId).pitch - 1) * 100 > range + 1e-9) return;
		}
		await runPerformanceCommandFromUi({ type: 'pitch_range', deck: deckId, range });
	}
	async function unloadDeck(): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'unload', deck: deckId });
	}

	// Library-row drop target. This is the ONLY track-drop path on a deck.
	// Acceptance comes from the in-app drag state, never from
	// dataTransfer.types: WKWebView hides custom MIME types during dragover, so
	// a types gate stops preventDefault from running and ondrop never fires.
	let dropHover = $state(false);

	function onTrackDragOver(event: DragEvent): void {
		if (!acceptTrackDragOver(event)) return;
		dropHover = true;
	}

	function onTrackDragLeave(event: DragEvent): void {
		const next = event.relatedTarget;
		if (next instanceof Node && event.currentTarget instanceof Node && event.currentTarget.contains(next)) {
			return;
		}
		dropHover = false;
	}

	async function onTrackDrop(event: DragEvent): Promise<void> {
		dropHover = false;
		const stableId = primaryDroppedStableId(event);
		const row = stableId !== null ? droppedRowFlags(stableId) : null;
		// End here as well as on dragend: acceptance now depends on the state,
		// so a dragend WebKit fails to deliver would leave every deck armed.
		endTrackDrag();
		if (stableId === null) return;
		event.preventDefault();
		await applyDeckTrackDrop({ deckId, occupied: deck.stable_id !== null, stableId, row, dispatch: dispatchPerformanceCommand, toast: pushToast });
	}

	// DECKUX-8360: decks 2+4 follow the right mirror pref, decks 1+3 the left.
	// A mirrored deck is a true horizontal mirror of the header and the main
	// row, so its sync, jog and transport sit on the screen-centre side.
	const mirrored: boolean = $derived(
		(deckId === 2 || deckId === 4) ? uiPrefs.deck_right_mirror : uiPrefs.deck_left_mirror
	);
</script>

<section
	class="rb-deck rb-panel"
	role="group"
	aria-label={`deck ${deckId}`}
	class:drop-hover={dropHover}
	class:loading={pending}
	class:deck-focus={deckHoverLinkId(uiPrefs.deck_hover_link) === deckId}
	class:selected={selected}
	class:is-master={deck.is_master}
	class:deck-dim={deckLook.dim}
	class:deck-looping={deckLook.looping}
	data-deck={deckId}
	data-deck-hover={deckId}
	use:selectorNode={{ id: selId.deck(deckId), kind: 'group', enabled: deckSelectable }}
	data-command-pending={pending}
	use:wheelAdjust={{
		step: WHEEL_STEP.fader,
		get: () => mixerState.channels[deckId].fader,
		set: handleDeckWheelAdjust
	}}
	onclick={handleDeckClick}
	onpointerenter={(e) => deckHoverEnter(deckIdFromHoverEl(e.currentTarget) ?? deckId)}
	onpointerleave={(e) => deckHoverLeave(deckIdFromHoverEl(e.currentTarget) ?? deckId, e)}
	ondragover={onTrackDragOver}
	ondragleave={onTrackDragLeave}
	ondrop={(e) => void onTrackDrop(e)}
>
	{#if reactDeck}
		<ReactIsland load={() => import('./deck/PadStrip.tsx')} props={{}} />
	{:else}
		<PadStrip />
	{/if}

	<DeckHeader
		{deck}
		{deckId}
		{pending}
		onBeatSync={toggleBeatSync}
		onMaster={selectMaster}
		onMasterTempo={toggleMasterTempo}
		onKeySync={syncKey}
		onKeyNudge={nudgeKey}
		onResetTempo={setTempo}
		onUnload={unloadDeck}
		{keySyncAvailable}
		{mirrored}
	/>

	<StripWaveform {deck} {pending} onSeek={seekTo} onPlay={playPause} />

	<div
		class="main-row"
		class:mirror-main-row={mirrored}
		data-mirrored={mirrored}
	>
		<!-- Left edge: grid adjust (top) and grid shift (bottom), GRIDEDIT-6235, then
		     the HOT CUE pad-mode menu as a two-line button (LYR-14), which frees the
		     row under the pads for the lyric strip. -->
		<div class="left-edge">
			<div class="prio-slot" data-priority="2">
				<GridEditButtons
					{deck}
					{deckId}
					press={(edit) => void runPerformanceCommandFromUi({ type: 'grid_edit', deck: deckId, edit })}
					reportError={(message: string) => pushToast(message, 'error')}
				/>
				{#if reactDeck}
					<ReactIsland load={() => import('./deck/PadModeMenu.tsx')} props={{ deckId, stacked: true }} />
				{:else}
					<PadModeMenu {deckId} stacked />
				{/if}
			</div>
		</div>

		<!-- The cue column absorbs spare width to preserve control alignment.
		     LYR-14: the 2x4 bank sits on top and the lyric strip runs under it
		     at the bank's full width, so a lyric line reads left to right. -->
		<div class="cue-flex" bind:this={cueFlexEl} use:selectorNode={{ id: selId.deckPart(deckId, 'cues'), kind: 'leaf', enabled: true }}>
			<HotCueBank
				{deck}
				{pending}
				onJump={triggerHotCue}
				onSave={hotCueActions.saveHotCueAt}
				onRename={hotCueActions.renameHotCueAt}
				onDelete={hotCueActions.clearHotCueAt}
				onRestore={hotCueActions.restoreHotCueAt}
			/>
			{#if uiPrefs.lyrics_deck_line && uiPrefs.lyrics_global && deck.stable_id !== null}
				<div class="deck-lyric-host" style:height={`${deckLyricStripHeight}px`}>
					<DeckLyricLine
						track={deckLyrics.track}
						entryState={deckLyricEntry?.state ?? 'loading'}
						error={deckLyrics.error}
						positionSource={presentedPositionSec}
						rows={deckLyricRows}
					/>
				</div>
			{/if}
		</div>

		<!-- Keep the compact transport modifiers together, outside the cue bank. -->
		<div class="loop-col" data-priority="2" use:selectorNode={{ id: selId.deckPart(deckId, 'loop'), kind: 'leaf', enabled: true }}>
			<LoopCluster {deck} {deckId} {pending} onEngage={engageBeatLoop} onDisengage={disengageLoop} onSafetySave={saveSafetyLoop} onSafetyArm={armSafetyLoop} onSafetyClear={clearSafetyLoop} onIntervalMode={setLoopIntervalMode} onIntervalBase={setLoopIntervalBase} onStoredLoop={engageStoredLoop} onLoopIn={loopIn} onLoopOut={loopOut} onReloop={reloop} onSetActive={setActiveMemoryLoop} />
			<div class="prio-slot" data-priority="3">
				<BeatJump {deck} {pending} onJump={beatJump} onSize={setBeatJumpSize} />
			</div>
		</div>

		<div class="sel-node" use:selectorNode={{ id: selId.deckPart(deckId, 'transport'), kind: 'leaf', enabled: true }}>
			<TransportCluster
				{deck}
				{pending}
				quantizedLaunchArmed={queryQuantizedLaunchArmed(deckId)}
				onCue={returnToCue}
				onMemoryCueCall={callMemoryCue}
				onPlayPause={playPause}
			/>
		</div>

		<div class="sel-node" use:selectorNode={{ id: selId.deckPart(deckId, 'jog'), kind: 'leaf', enabled: true }}>
			<JogDial
				{deck}
				{pitchRange}
				{pending}
				onQuantize={toggleQuantize}
				onQuantizeGrid={setQuantizeGrid}
				onMasterTempo={toggleMasterTempo}
				onAutoCue={(enabled) => void runPerformanceCommandFromUi({ type: 'deck_autocue', enabled })}
				onSlip={toggleSlip}
			/>
		</div>

		<div class="sel-node" use:selectorNode={{ id: selId.deckPart(deckId, 'pitch'), kind: 'leaf', enabled: true }}>
			<PitchFader
				{deck}
				{pitchRange}
				{pending}
				onTempoChange={setTempo}
				onRangeChange={setPitchRangeUi}
			/>
		</div>
	</div>

	<div class="sel-node" use:selectorNode={{ id: selId.deckPart(deckId, 'stems'), kind: 'leaf', enabled: true }}>
		{#if reactDeck}
			<ReactIsland
				load={() => import('./deck/StemRow.tsx')}
				props={{
					deck: stemRowDeck,
					pending,
					onMute: toggleStemMute,
					onSolo: toggleStemSolo,
					onLoad: loadStems,
					separate: separateView,
					onSeparate: separateStems,
					onQuickMode: applyStemQuickMode
				}}
			/>
		{:else}
			<StemRow
				{deck}
				{pending}
				onMute={toggleStemMute}
				onSolo={toggleStemSolo}
				onLoad={loadStems}
				separate={separateView}
				onSeparate={separateStems}
				onQuickMode={applyStemQuickMode}
				stemsResetOn={trackLoadResetEnabled('stems')}
				onOpenResetConfig={(el) => toggleTrackLoadResetPanel('stems', el, deck.deck_id)}
			/>
		{/if}
	</div>

	{#if controlError !== null}
		{#if reactDeck}
			<ReactIsland
				load={() => import('./deck/DeckErrorBanner.tsx')}
				props={{ deckId, error: controlError, tone: deckBannerTone(controlError), errorId: controlErrorId, onDismiss: () => dismissPerformanceDeckError(deckId) }}
			/>
		{:else}
			<DeckErrorBanner
				{deckId}
				error={controlError}
				tone={deckBannerTone(controlError)}
				errorId={controlErrorId}
				onDismiss={() => dismissPerformanceDeckError(deckId)}
			/>
		{/if}
	{/if}

	{#if reactDeck}
		<ReactIsland
			load={() => import('./deck/SecondaryLoadBadge.tsx')}
			props={{ status: deck.stems.status, error: deck.stems.error, phase: deck.stems.load?.phase ?? null }}
		/>
	{:else}
		<SecondaryLoadBadge status={deck.stems.status} error={deck.stems.error} phase={deck.stems.load?.phase ?? null} />
	{/if}

	{#if deck.last_load_latency_ms !== null}
		<span
			class="load-latency"
			title="Last deck load wall time (warm cache should drop fetchAudio ~1s)"
		>
			{formatLoadLatency(deck.last_load_latency_ms)}
		</span>
	{/if}
</section>

<style>
	.rb-deck {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 4px 8px;
		min-height: 0;
		min-width: 0;
		flex: 1;
		overflow: hidden;
		/* single inset stroke - avoids .rb-panel border doubling against
		 * column chrome / stacked neighbour decks */
		border: none;
		box-shadow: inset 0 0 0 1px #3d4652;
	}
	.load-latency {
		position: absolute;
		right: 6px;
		bottom: 3px;
		z-index: 2;
		font-size: 9px;
		line-height: 1;
		letter-spacing: 0.02em;
		color: color-mix(in srgb, var(--rb-text-dim, #8b93a0) 85%, transparent);
		pointer-events: none;
		user-select: none;
	}
	.rb-deck.drop-hover {
		outline: 1px solid var(--rb-accent);
		outline-offset: -1px;
		background: color-mix(in srgb, var(--rb-accent) 10%, var(--rb-panel));
	}
	/* Bottom-left → top-right white pulse while a command (load) is in flight. */
	.rb-deck.loading::after {
		content: '';
		position: absolute;
		inset: 0;
		z-index: 6;
		pointer-events: none;
		background: linear-gradient(
			135deg,
			transparent 0%,
			transparent 42%,
			rgba(255, 255, 255, 0.07) 50%,
			transparent 58%,
			transparent 100%
		);
		background-size: 220% 220%;
		animation: deck-load-sweep 1.25s ease-in-out infinite;
	}
	@keyframes deck-load-sweep {
		0% {
			background-position: 100% 100%;
			opacity: 0.55;
		}
		50% {
			opacity: 1;
		}
		100% {
			background-position: 0% 0%;
			opacity: 0.55;
		}
	}
	/* DECKUX-5917: the link tick layers over each resting stroke, never replaces it. */
	.rb-deck.deck-focus {
		box-shadow:
			var(--rb-deck-link-tick),
			inset 0 0 0 1px #3d4652;
	}
	.rb-deck.selected {
		box-shadow: inset 0 0 0 0.5px rgba(255, 255, 255, 0.12);
	}
	.rb-deck.selected.deck-focus {
		box-shadow:
			var(--rb-deck-link-tick),
			inset 0 0 0 0.5px rgba(255, 255, 255, 0.12);
	}
	/* Yellow master outline stays for quick ID. */
	.rb-deck.is-master {
		box-shadow: inset 0 0 0 2px var(--rb-yellow);
	}
	.rb-deck.is-master.deck-focus {
		box-shadow:
			var(--rb-deck-link-tick),
			inset 0 0 0 2px var(--rb-yellow);
	}
	/* Finished, or paused longer than 30s: pull the whole panel back so a
	   deck that is actually playing stays the bright one. A veil, not a
	   filter: a filter would make the deck the containing block for its
	   fixed-position menus (tempo editor, loop source, explainer) and clip
	   them. Same layer as the loop veil, so deck menus stay undimmed. */
	.rb-deck.deck-dim::before {
		content: '';
		position: absolute;
		inset: 0;
		z-index: 85;
		pointer-events: none;
		background: rgba(0, 0, 0, 0.42);
	}
	/* Engaged loop. A veil, not a background swap, so opaque controls tint too.
	   Sits under deck menus (z-index 1000) and above transport (z-index 80). */
	.rb-deck.deck-looping::before {
		content: '';
		position: absolute;
		inset: 0;
		z-index: 85;
		pointer-events: none;
		background: color-mix(in srgb, var(--rb-orange) 14%, transparent);
	}
	.rb-deck.deck-dim.deck-looping::before {
		background:
			linear-gradient(color-mix(in srgb, var(--rb-orange) 14%, transparent), color-mix(in srgb, var(--rb-orange) 14%, transparent)),
			rgba(0, 0, 0, 0.42);
	}
	.main-row {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		flex: 1 1 auto;
		min-height: 0;
		min-width: 0;
		overflow: hidden;
	}
	/* DECKUX-8360: a true mirror. The row order reverses, and so do the two
	   side-by-side groups inside it (loop | jump, dial | Q/SLIP/MT column), so
	   the jog's modifier column and the transport face the screen centre the
	   way they do on an unmirrored deck. Each control's own content (labels,
	   pad grid, text) keeps its normal left-to-right order. */
	.main-row.mirror-main-row {
		flex-direction: row-reverse;
	}
	.main-row.mirror-main-row > .loop-col,
	.main-row.mirror-main-row > .sel-node > :global(.jog) {
		flex-direction: row-reverse;
	}
	.left-edge {
		display: flex;
		flex-direction: column;
		gap: 3px;
		flex: 0 0 auto;
	}
	.loop-col {
		display: flex;
		flex-direction: row;
		align-items: flex-start;
		gap: 3px;
		flex: 0 0 auto;
		min-height: 0;
		/* This group stays beside the cue bank within main-row's fixed height.
		 * LoopCluster keeps its own
		 * fixed 44px width and vertical controls. */
	}
	/* SKINLAYOUT-10203: deck tiers by container query, never @media
	 * (PLAN.md 2.4). Optional parts carry data-priority and drop lowest first.
	 * compact < 360 px: artwork, pad labels, BeatJump (priority 3).
	 * narrow  < 300 px: loop column and grid-edit buttons too (priority 2).
	 * Nothing scales fonts. */
	.prio-slot {
		display: contents;
	}
	@container deck (max-width: 359px) {
		.rb-deck :global([data-priority='3']) {
			display: none;
		}
	}
	@container deck (max-width: 299px) {
		.rb-deck :global([data-priority='2']) {
			display: none;
		}
	}
	.cue-flex {
		flex: 1 1 0;
		min-width: 0;
		align-self: stretch;
		display: flex;
		flex-direction: column;
		gap: 3px;
		position: relative;
	}
	/* LYR-14: HotCueBank's UNDO button (shown after a save, rename or
	   delete) would otherwise stack under the pads and starve the lyric
	   strip to ~12px. Float it at the column's bottom-right corner instead,
	   and narrow the lyric strip by its width while it is up, so neither
	   control covers the other and the strip keeps its rows. */
	.cue-flex > :global(.cue-area > .undo) {
		position: absolute;
		right: 0;
		bottom: 0;
		width: var(--deck-undo-w, 56px);
		z-index: 1;
	}
	.cue-flex:has(> :global(.cue-area > .undo)) > .deck-lyric-host {
		padding-right: calc(var(--deck-undo-w, 56px) + 4px);
	}
	/* LYR-14: the bank takes the column's full width and never shrinks below
	   its pads, so no pad border is clipped (ui-border-audit). HotCueBank's
	   root carries align-self: flex-start, which in this column would shrink
	   it to its content width, so stretch it here. The lyric strip is the
	   side that gives: when the column is a pixel short of pads plus strip,
	   the strip shrinks and its line fit re-measures. */
	.cue-flex > :global(.cue-area) {
		flex: 1 0 auto;
		min-height: 0;
		width: auto;
		align-self: stretch;
	}
	.deck-lyric-host {
		flex: 0 1 auto;
		min-height: 0;
		min-width: 0;
		display: flex;
		align-items: stretch;
		overflow: hidden;
	}
</style>
