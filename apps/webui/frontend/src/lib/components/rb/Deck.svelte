<script lang="ts">
	// Build unit: deck (COMPONENT-MAP 1.3, SCREENSHOT-SPEC 3).
	// REAL: header meta + artwork, strip overview waveform click-to-seek,
	// hot-cue bank jumps, INT beat-loop cluster, CUE + play/pause transport,
	// Q, BEAT SYNC, MASTER, MT, KEY SYNC, key nudge, SLIP, jog readouts + position tick.
	// INERT (tooltip 'not implemented - see PARITY-TODO'): HOT CUE dropdown, grid-adjust stacks, AU, MA, pitch range.
	// Stems are live only for validated real Demucs artifacts.
	//
	// ALL live state comes from the audio-engine accessor: the engine unit
	// owns DeckState (types.ts) via the rune module audio-engine.svelte.ts.
	import {
		DECK_IDS,
		deckStates,
		engine,
		getDeckState,
		parseCamelotKey,
		pitchRanges
	} from '$lib/rb/audio-engine.svelte';
	import type { PitchRange } from '$lib/rb/audio-engine.svelte';
	import { clearHotCue, saveHotCue } from '$lib/rb/api-rb';
	import { quantizeToNearestBeat } from '$lib/rb/beat-sync-math';
	import {
		performanceCommandStatus,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import type { DeckId, DeckState, HotCueSlot, StemControl } from '$lib/rb/types';
	import DeckHeader from './deck/DeckHeader.svelte';
	import HotCueBank from './deck/HotCueBank.svelte';
	import JogDial from './deck/JogDial.svelte';
	import LoopCluster from './deck/LoopCluster.svelte';
	import StemRow from './deck/StemRow.svelte';
	import StripWaveform from './deck/StripWaveform.svelte';
	import TransportCluster from './deck/TransportCluster.svelte';

	let { deckId }: { deckId: DeckId } = $props();

	const deck: DeckState = $derived(getDeckState(deckId));
	const pitchRange: PitchRange = $derived(pitchRanges[deckId]);
	const pending: boolean = $derived(performanceCommandStatus.deck_pending[deckId] > 0);
	const controlError: string | null = $derived(
		performanceCommandStatus.deck_errors[deckId] ?? deck.sync_error ?? deck.processor_error
	);
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

	const INERT_TIP = 'not implemented - see PARITY-TODO';
	const PAD_LETTERS: string[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

	// ------------------------------------------------- engine call plumbing
	// Engine methods throw loudly on empty decks (fail-fast contract);
	// callers below are gated by disabled states, never by silent catches.

	async function seekTo(ms: number): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'seek', deck: deckId, position_ms: ms });
	}

	async function playPause(): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'play', deck: deckId, playing: !deck.playing });
	}

	async function returnToCue(): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'cue', deck: deckId });
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

	async function toggleQuantize(): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'quantize',
			deck: deckId,
			enabled: !deck.quantize_enabled
		});
	}

	async function toggleBeatSync(): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'beat_sync',
			deck: deckId,
			enabled: !deck.beat_sync_enabled
		});
	}

	async function selectMaster(): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'master', deck: deckId });
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
		await runPerformanceCommandFromUi({ type: 'key_sync', deck: deckId });
	}

	async function nudgeKey(semitones: -1 | 1): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'key_nudge', deck: deckId, semitones });
	}

	// ------------------------------------------- hot-cue SAVE / CLEAR
	// Persistence writes (djmdCue Kind 1-8), not performance commands -
	// they go straight to the REST write surface, then refresh the deck's
	// hot_cues from the backend (rb_vendor.fetch_cues is always live).

	async function saveHotCueAt(slot: HotCueSlot): Promise<void> {
		const stableId = deck.stable_id;
		if (stableId === null) return;
		let ms = deck.position_ms;
		const beats = deck.anlz?.beatgrid.beats ?? [];
		if (deck.quantize_enabled && beats.length > 0) {
			ms = Math.round(quantizeToNearestBeat(beats, ms / 1000) * 1000);
		}
		try {
			await saveHotCue(stableId, slot, ms);
			await engine.refreshHotCues(deckId);
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`Hot cue ${slot} save failed - ${message}`, 'error');
		}
	}

	async function clearHotCueAt(slot: HotCueSlot): Promise<void> {
		const stableId = deck.stable_id;
		if (stableId === null) return;
		try {
			await clearHotCue(stableId, slot);
			await engine.refreshHotCues(deckId);
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`Hot cue ${slot} clear failed - ${message}`, 'error');
		}
	}
</script>

<section class="rb-deck rb-panel" data-deck={deckId} data-command-pending={pending}>
	<!-- Performance pad letter strip along the panel top edge (static echo
	     of the hot-cue bank, SCREENSHOT-SPEC 3). -->
	<div class="pad-strip" aria-hidden="true">
		{#each PAD_LETTERS as letter, i (letter)}
			{#if i === 4}
				<span class="pad-sep">|</span>
			{/if}
			<span class="pad-letter">{letter}</span>
		{/each}
	</div>

	<DeckHeader
		{deck}
		{deckId}
		{pending}
		onBeatSync={toggleBeatSync}
		onMaster={selectMaster}
		onKeySync={syncKey}
		onKeyNudge={nudgeKey}
		{keySyncAvailable}
	/>

	<StripWaveform {deck} {pending} onSeek={seekTo} />

	<div class="main-row">
		<!-- Left edge: 2 grid-adjust icon stacks (inert, COMPONENT-MAP 1.3). -->
		<div class="grid-adjust">
			<button class="rb-lit-button rb-inert" disabled title={INERT_TIP} aria-label="grid adjust">
				<span class="ticks">&#9475;&#9475;&#9475;</span>
			</button>
			<button class="rb-lit-button rb-inert" disabled title={INERT_TIP} aria-label="grid shift">
				<span class="ticks">&#9478;&#9478;&#9478;</span>
			</button>
		</div>

		<!-- The cue bank is the deck panel's flexible middle (wide slot rows,
		     SCREENSHOT-SPEC 3 wide layout) - it absorbs all spare width. -->
		<div class="cue-flex">
			<HotCueBank
				{deck}
				{pending}
				onJump={seekTo}
				onSave={saveHotCueAt}
				onDelete={clearHotCueAt}
				inertTip={INERT_TIP}
			/>
		</div>

		<LoopCluster
			{deck}
			{pending}
			onEngage={engageBeatLoop}
			onDisengage={disengageLoop}
			inertTip={INERT_TIP}
		/>

		<TransportCluster {deck} {pending} onCue={returnToCue} onPlayPause={playPause} />

		<JogDial
			{deck}
			{pitchRange}
			{pending}
			onQuantize={toggleQuantize}
			onMasterTempo={toggleMasterTempo}
			onSlip={toggleSlip}
			inertTip={INERT_TIP}
		/>
	</div>

	<StemRow
		{deck}
		{pending}
		onMute={toggleStemMute}
		onSolo={toggleStemSolo}
	/>

	{#if controlError !== null}
		<div class="deck-error" role="alert" data-performance-error={deckId} title={controlError}>
			{controlError}
		</div>
	{/if}
</section>

<style>
	.rb-deck {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 6px 8px;
		min-height: 0;
		min-width: 0;
		flex: 1;
		overflow: hidden;
	}
	.pad-strip {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 10px;
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		line-height: 1;
	}
	.pad-sep {
		color: var(--rb-border);
	}
	.main-row {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1 1 auto;
		min-height: 0;
		min-width: 0;
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
	.cue-flex {
		flex: 1 1 0;
		min-width: 0;
		align-self: stretch;
		display: flex;
	}
	.deck-error {
		position: absolute;
		z-index: 4;
		left: 8px;
		right: 8px;
		bottom: 3px;
		padding: 2px 5px;
		border: 1px solid var(--rb-red);
		background: rgba(30, 5, 5, 0.94);
		color: #ff8e87;
		font-size: var(--rb-fs-label);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>
