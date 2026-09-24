<script lang="ts">
	// Build unit: deck (COMPONENT-MAP 1.3, SCREENSHOT-SPEC 3).
	// REAL: header meta + artwork, strip overview waveform click-to-seek,
	// hot-cue bank jumps, INT beat-loop cluster, CUE + play/pause transport,
	// Q, BEAT SYNC, MASTER, MT, KEY SYNC, key nudge, SLIP, jog readouts +
	// position tick, pitch fader + 8/16/WIDE range switcher. Stems are live
	// only for validated real Demucs artifacts.
	// INERT (plannedTitle per control): HOT CUE dropdown, grid-adjust stacks,
	// AU, MA.
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
		mixerState,
		parseCamelotKey,
		pitchRanges
	} from '$lib/rb/audio-engine.svelte';
	import type { PitchRange } from '$lib/rb/audio-engine.svelte';
	import { deckErrorIds, noteDeckError } from '$lib/rb/deck-error-id.svelte';
	import { createDeckHotCueActions } from '$lib/rb/deck-hot-cue-actions';
	import {
		performanceCommandStatus,
		dismissPerformanceDeckError,
		dispatchPerformanceCommand,
		queryPerformanceState,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import {
		deckHoverEnter,
		deckHoverLeave,
		deckHoverUi,
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
	import type { DeckState } from '$lib/rb/deck-state-types';
	import type { HotCueSlot } from '$lib/rb/hot-cue-types';
	import type { StemControl } from '$lib/rb/stem-types';
	import BeatJump from './deck/BeatJump.svelte';
	import DeckHeader from './deck/DeckHeader.svelte';
	import HotCueBank from './deck/HotCueBank.svelte';
	import JogDial from './deck/JogDial.svelte';
	import LoopCluster from './deck/LoopCluster.svelte';
	import PadStrip from './deck/PadStrip.svelte';
	import PitchFader from './deck/PitchFader.svelte';
	import DeckErrorBanner from './deck/DeckErrorBanner.svelte';
	import DeckLyricLine from './deck/DeckLyricLine.svelte';
	import SecondaryLoadBadge from './deck/SecondaryLoadBadge.svelte';
	import StemRow from './deck/StemRow.svelte';
	import StripWaveform from './deck/StripWaveform.svelte';
	import TransportCluster from './deck/TransportCluster.svelte';
	import { plannedTitle } from '$lib/rb/planned-explainers';

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
	const controlError: string | null = $derived(
		performanceCommandStatus.deck_errors[deckId] ?? deck.sync_error ?? deck.processor_error
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

	// ------------------------------------------------- engine call plumbing
	// Engine methods throw loudly on empty decks (fail-fast contract);
	// callers below are gated by disabled states, never by silent catches.

	async function seekTo(ms: number): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'seek', deck: deckId, position_ms: ms });
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

	async function engageBeatLoop(beats: number, startMs?: number): Promise<void> {
		await runPerformanceCommandFromUi(
			startMs === undefined
				? { type: 'beat_loop', deck: deckId, beats }
				: { type: 'beat_loop', deck: deckId, beats, start_ms: startMs }
		);
	}

	async function disengageLoop(): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'loop', deck: deckId, loop: null });
	}

	async function beatJump(beats: number): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'beat_jump', deck: deckId, beats });
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

	async function toggleQuantize(): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'quantize',
			deck: deckId,
			enabled: !deck.quantize_enabled
		});
	}

	/** Pin a67bafbfc4b0: change the quantize GRID (1/4/8 beats). 'phase' is
	 * plumbed but not implemented, so it never reaches here - JogDial's own
	 * phase option is disabled and calls nothing. */
	async function setQuantizeGrid(beats: 1 | 4 | 8): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'quantize_grid', deck: deckId, beats });
	}

	async function toggleBeatSync(): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'beat_sync',
			deck: deckId,
			enabled: !deck.beat_sync_enabled
		});
	}

	async function selectMaster(): Promise<void> {
		const state = queryPerformanceState();
		if (state.master_deck === deckId && state.master_mode === 'locked') {
			await runPerformanceCommandFromUi({ type: 'master', deck: deckId, lock: false });
			return;
		}
		await runPerformanceCommandFromUi({ type: 'master', deck: deckId, lock: true });
	}

	async function toggleMasterTempo(): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'master_tempo',
			deck: deckId,
			enabled: !deck.master_tempo_enabled
		});
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

	async function nudgeKey(semitones: -1 | 1): Promise<void> {
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
</script>

<section
	class="rb-deck rb-panel"
	role="group"
	aria-label={`deck ${deckId}`}
	class:drop-hover={dropHover}
	class:loading={pending}
	class:deck-focus={deckHoverUi.deckId === deckId}
	class:selected={selected}
	class:is-master={deck.is_master}
	data-deck={deckId}
	data-deck-hover={deckId}
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
	<PadStrip />

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
	/>

	<StripWaveform {deck} {pending} onSeek={seekTo} onPlay={playPause} />

	<div class="main-row">
		<!-- Left edge: 2 grid-adjust icon stacks (inert, COMPONENT-MAP 1.3). -->
		<div class="grid-adjust">
			<button class="rb-lit-button rb-inert" disabled title={plannedTitle('grid-adjust')} aria-label={`grid adjust deck ${deckId}`} data-testid={`grid-adjust-deck-${deckId}`}>
				<span class="ticks">&#9475;&#9475;&#9475;</span>
			</button>
			<button class="rb-lit-button rb-inert" disabled title={plannedTitle('grid-shift')} aria-label={`grid shift deck ${deckId}`} data-testid={`grid-shift-deck-${deckId}`}>
				<span class="ticks">&#9478;&#9478;&#9478;</span>
			</button>
		</div>

		<!-- The cue host and 2x4 bank absorb spare width to preserve control
		     alignment; the cue rows stay vertically bounded. -->
		<div class="cue-flex">
			<HotCueBank
				{deck}
				{pending}
				onJump={triggerHotCue}
				onSave={hotCueActions.saveHotCueAt}
				onRename={hotCueActions.renameHotCueAt}
				onDelete={hotCueActions.clearHotCueAt}
				onRestore={hotCueActions.restoreHotCueAt}
			/>
		</div>

		<!-- Keep the compact transport modifiers together, outside the cue bank. -->
		<div class="loop-col">
			<LoopCluster {deck} {deckId} {pending} onEngage={engageBeatLoop} onDisengage={disengageLoop} onSafetySave={saveSafetyLoop} onSafetyArm={armSafetyLoop} onSafetyClear={clearSafetyLoop} onIntervalMode={setLoopIntervalMode} onIntervalBase={setLoopIntervalBase} />
			<BeatJump {deck} {pending} onJump={beatJump} />
		</div>

		<TransportCluster
			{deck}
			{pending}
			quantizedLaunchArmed={queryPerformanceState().decks[deckId].quantized_launch_armed}
			onCue={returnToCue}
			onPlayPause={playPause}
		/>

		<JogDial
			{deck}
			{pitchRange}
			{pending}
			onQuantize={toggleQuantize}
			onQuantizeGrid={setQuantizeGrid}
			onMasterTempo={toggleMasterTempo}
			onSlip={toggleSlip}
		/>

		<PitchFader
			{deck}
			{pitchRange}
			{pending}
			onTempoChange={setTempo}
			onRangeChange={setPitchRangeUi}
		/>
	</div>

	{#if uiPrefs.lyrics_deck_line && uiPrefs.lyrics_global && deck.stable_id !== null && deckLyricEntry !== null}
		<DeckLyricLine
			track={deckLyrics.track}
			entryState={deckLyricEntry.state}
			error={deckLyrics.error}
			positionSource={presentedPositionSec}
			rows={1}
		/>
	{/if}

	<StemRow
		{deck}
		{pending}
		onMute={toggleStemMute}
		onSolo={toggleStemSolo}
	/>

	{#if controlError !== null}
		<DeckErrorBanner
			{deckId}
			error={controlError}
			errorId={controlErrorId}
			onDismiss={() => dismissPerformanceDeckError(deckId)}
		/>
	{/if}

	<SecondaryLoadBadge status={deck.stems.status} error={deck.stems.error} />

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
	.rb-deck.deck-focus {
		transition:
			box-shadow 50ms ease-out,
			background 50ms ease-out;
		background: radial-gradient(
			ellipse 90% 80% at 50% 40%,
			rgba(255, 255, 255, 0.07) 0%,
			transparent 70%
		);
		box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.18);
	}
	.rb-deck.selected {
		box-shadow: inset 0 0 0 0.5px rgba(255, 255, 255, 0.12);
	}
	.rb-deck.selected.deck-focus {
		box-shadow: inset 0 0 0 0.5px rgba(255, 255, 255, 0.12);
	}
	/* Yellow master outline wins over focus stroke for quick ID. */
	.rb-deck.is-master {
		box-shadow: inset 0 0 0 2px var(--rb-yellow);
	}
	.rb-deck.is-master.deck-focus {
		box-shadow: inset 0 0 0 2px var(--rb-yellow);
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
	.grid-adjust {
		display: flex;
		flex-direction: column;
		gap: 3px;
		flex: 0 0 auto;
	}
	.grid-adjust button {
		padding: 2px 3px;
	}
	.ticks {
		font-size: 8px;
		letter-spacing: 1px;
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
	.cue-flex {
		flex: 1 1 0;
		min-width: 0;
		align-self: stretch;
		display: flex;
	}
</style>
