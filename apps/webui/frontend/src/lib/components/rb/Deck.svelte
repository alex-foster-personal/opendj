<script lang="ts">
	// Build unit: deck (COMPONENT-MAP 1.3, SCREENSHOT-SPEC 3).
	// REAL: header meta + artwork, strip overview waveform click-to-seek,
	// hot-cue bank jumps, INT beat-loop cluster, CUE + play/pause transport,
	// Q, BEAT SYNC, MASTER, MT, KEY SYNC, key nudge, SLIP, jog readouts +
	// position tick, pitch fader + 8/16/WIDE range switcher. Stems are live
	// only for validated real Demucs artifacts.
	// INERT (tooltip 'not implemented - see PARITY-TODO'): HOT CUE dropdown,
	// grid-adjust stacks, AU, MA.
	//
	// ALL live state comes from the audio-engine accessor: the engine unit
	// owns DeckState (types.ts) via the rune module audio-engine.svelte.ts.
	import {
	DECK_IDS,
	deckStates,
	getDeckState,
		parseCamelotKey,
		pitchRanges
	} from '$lib/rb/audio-engine.svelte';
	import type { PitchRange } from '$lib/rb/audio-engine.svelte';
	import type { HotCueMutation } from '$lib/rb/api-rb';
	import { quantizeToNearestBeat } from '$lib/rb/beat-sync-math';
	import {
		performanceCommandStatus,
		dispatchPerformanceCommand,
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
	import type { DeckId, DeckState, HotCueSlot, StemControl } from '$lib/rb/types';
	import DeckHeader from './deck/DeckHeader.svelte';
	import HotCueBank from './deck/HotCueBank.svelte';
	import JogDial from './deck/JogDial.svelte';
	import LoopCluster from './deck/LoopCluster.svelte';
	import PitchFader from './deck/PitchFader.svelte';
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

	async function saveHotCueAt(slot: HotCueSlot): Promise<HotCueMutation> {
		const stableId = deck.stable_id;
		if (stableId === null) throw new Error(`hot cue ${slot}: deck is not loaded`);
		const revision = deck.hot_cue_revisions[slot];
		if (!revision) throw new Error(`hot cue ${slot}: slot revision is unavailable`);
		let ms = deck.position_ms;
		const beats = deck.anlz?.beatgrid.beats ?? [];
		if (deck.quantize_enabled && beats.length > 0) {
			ms = Math.round(quantizeToNearestBeat(beats, ms / 1000) * 1000);
		}
		try {
			const state = await dispatchPerformanceCommand({
				type: 'hot_cue_save', deck: deckId, slot, in_ms: ms, revision
			});
			const reversal = state.decks[deckId].hot_cue_reversal;
			if (reversal === null) throw new Error(`hot cue ${slot}: dispatcher omitted reversal token`);
			return { cue: null, revision: reversal.revision, reversal: { reversal_id: reversal.reversal_id } };
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`Hot cue ${slot} save failed - ${message}`, 'error');
			throw error;
		}
	}

	async function clearHotCueAt(slot: HotCueSlot): Promise<HotCueMutation> {
		const stableId = deck.stable_id;
		if (stableId === null) throw new Error(`hot cue ${slot}: deck is not loaded`);
		const revision = deck.hot_cue_revisions[slot];
		if (!revision) throw new Error(`hot cue ${slot}: slot revision is unavailable`);
		try {
			const state = await dispatchPerformanceCommand({
				type: 'hot_cue_clear', deck: deckId, slot, revision
			});
			const reversal = state.decks[deckId].hot_cue_reversal;
			if (reversal === null) throw new Error(`hot cue ${slot}: dispatcher omitted reversal token`);
			return { cue: null, revision: reversal.revision, reversal: { reversal_id: reversal.reversal_id } };
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`Hot cue ${slot} clear failed - ${message}`, 'error');
			throw error;
		}
	}

	async function restoreHotCueAt(
		slot: HotCueSlot,
		revision: string,
		reversalId: string
	): Promise<void> {
		const stableId = deck.stable_id;
		if (stableId === null) throw new Error(`hot cue ${slot}: deck is not loaded`);
		try {
			await dispatchPerformanceCommand({
				type: 'hot_cue_restore', deck: deckId, slot, revision, reversal_id: reversalId
			});
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`Hot cue ${slot} restore failed - ${message}`, 'error');
			throw error;
		}
	}

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

	const MIME_TRACK = 'application/x-mdt-stable-id';
	let dropHover = $state(false);

	function onTrackDragOver(event: DragEvent): void {
		if (!event.dataTransfer?.types.includes(MIME_TRACK)) return;
		event.preventDefault();
		event.dataTransfer.dropEffect = 'copy';
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
		const raw = event.dataTransfer?.getData(MIME_TRACK)?.trim() ?? '';
		const stableId = raw.split(',')[0]?.trim() ?? '';
		if (stableId === '') return;
		event.preventDefault();
		try {
			if (deck.stable_id !== null) {
				await dispatchPerformanceCommand({ type: 'unload', deck: deckId });
			}
			await dispatchPerformanceCommand({ type: 'load', deck: deckId, stable_id: stableId });
		} catch (error: unknown) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`drop load failed: ${message}`, 'error');
		}
	}
</script>

<section
	class="rb-deck rb-panel"
	class:drop-hover={dropHover}
	class:loading={pending}
	class:deck-focus={deckHoverUi.deckId === deckId}
	class:is-master={deck.is_master}
	data-deck={deckId}
	data-deck-hover={deckId}
	data-command-pending={pending}
	onpointerenter={(e) => deckHoverEnter(deckIdFromHoverEl(e.currentTarget) ?? deckId)}
	onpointerleave={(e) => deckHoverLeave(deckIdFromHoverEl(e.currentTarget) ?? deckId, e)}
	ondragover={onTrackDragOver}
	ondragleave={onTrackDragLeave}
	ondrop={(e) => void onTrackDrop(e)}
>
	<!-- Decorative A-H pad letter strip (hidden by default via
	     --rb-pad-strip-display: none in theme.css). -->
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
		onUnload={unloadDeck}
		{keySyncAvailable}
	/>

	<StripWaveform {deck} {pending} onSeek={seekTo} onPlay={playPause} />

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

		<!-- The cue host and 2x4 bank absorb spare width to preserve control
		     alignment; the cue rows stay vertically bounded. -->
		<div class="cue-flex">
			<HotCueBank
				{deck}
				{pending}
				onJump={seekTo}
				onSave={saveHotCueAt}
				onDelete={clearHotCueAt}
				onRestore={restoreHotCueAt}
				inertTip={INERT_TIP}
			/>
		</div>

		<LoopCluster
			{deck}
			{deckId}
			{pending}
			onEngage={engageBeatLoop}
			onDisengage={disengageLoop}
			onSafetySave={saveSafetyLoop}
			onSafetyArm={armSafetyLoop}
			onSafetyClear={clearSafetyLoop}
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

		<PitchFader
			{deck}
			{pitchRange}
			{pending}
			onTempoChange={setTempo}
			onRangeChange={setPitchRangeUi}
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
	/* Yellow master outline wins over focus stroke for quick ID. */
	.rb-deck.is-master {
		box-shadow: inset 0 0 0 2px var(--rb-yellow);
	}
	.rb-deck.is-master.deck-focus {
		box-shadow: inset 0 0 0 2px var(--rb-yellow);
	}
	.pad-strip {
		/* Toggle: set --rb-pad-strip-display: none on .perf-root to hide. */
		display: var(--rb-pad-strip-display, flex);
		align-items: center;
		justify-content: center;
		gap: 10px;
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		line-height: 1;
		flex: 0 0 auto;
	}
	.pad-sep {
		color: var(--rb-border);
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
