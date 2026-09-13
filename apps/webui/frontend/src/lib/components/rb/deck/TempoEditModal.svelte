<script lang="ts">
	import { tick } from 'svelte';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';
	import {
		DECK_IDS,
		deckEffectiveBpm,
		deckStates,
		pitchRanges
	} from '$lib/rb/audio-engine.svelte';
	import { playbackBpm } from '$lib/rb/beat-sync-math';
	import { camelotKeyColor } from '$lib/rb/camelot-color';
	import { coalesceLatest } from '$lib/rb/coalesce';
	import type { DeckId } from '$lib/rb/deck-slots';
	import { gridFeatureInertTip, gridFeaturesInert } from '$lib/player/grid-features';
	import { keyAtPlayheadNow } from '$lib/player/key-playhead-lazy.svelte';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import {
		halveDoubleVisibility,
		nudgeBpm,
		parseTempoBpmInput,
		tempoWritesFromTargetBpm
	} from '$lib/rb/tempo-edit';
	import {
		faderValueFromPitchRatio,
		pitchRatioFromFaderValue
	} from './pitch-fader-geometry';

	let {
		deckId,
		x,
		y,
		onclose
	}: {
		deckId: DeckId;
		x: number;
		y: number;
		onclose: () => void;
	} = $props();

	let pane = $state<HTMLDivElement | null>(null);
	let position = $state({ x: 0, y: 0 });
	let skipBlurCommit = $state(false);
	let drafts = $state<Record<DeckId, string>>({ 1: '', 2: '', 3: '', 4: '' });

	const tempoDispatcher = coalesceLatest(async (ratio: number) => {
		await runPerformanceCommandFromUi({ type: 'tempo', deck: deckId, ratio });
	});

	$effect(() => {
		return () => tempoDispatcher.cancel();
	});

	const masterDeckId = $derived(DECK_IDS.find((candidate) => deckStates[candidate].is_master));
	const masterBpm = $derived(masterDeckId === undefined ? null : deckEffectiveBpm(masterDeckId));

	function _effectiveBpm(id: DeckId): number | null {
		const deck = deckStates[id];
		return playbackBpm({
			beats: deck.anlz?.beatgrid.beats,
			positionSec: Math.max(0, deck.position_ms / 1000),
			tempoRatio: deck.pitch,
			tagBpm: deck.bpm
		});
	}

	function _baseBpm(id: DeckId): number | null {
		const deck = deckStates[id];
		return playbackBpm({
			beats: deck.anlz?.beatgrid.beats,
			positionSec: Math.max(0, deck.position_ms / 1000),
			tempoRatio: 1,
			tagBpm: deck.bpm
		});
	}

	function _formatBpm(bpm: number | null): string {
		return bpm === null ? '--.--' : bpm.toFixed(2);
	}

	function _syncDraftsFromLive(): void {
		const next: Record<DeckId, string> = { 1: '', 2: '', 3: '', 4: '' };
		for (const id of DECK_IDS) {
			next[id] = _formatBpm(_effectiveBpm(id));
		}
		drafts = next;
	}

	async function placePane(): Promise<void> {
		_syncDraftsFromLive();
		await tick();
		if (pane === null) return;
		const rect = pane.getBoundingClientRect();
		position = clampToViewport(
			x,
			y,
			{ width: rect.width, height: rect.height },
			{ width: window.innerWidth, height: window.innerHeight }
		);
		const input = pane.querySelector<HTMLInputElement>(
			`[data-testid="tempo-edit-bpm-deck-${deckId}"]`
		);
		input?.focus();
		input?.select();
	}

	$effect(() => {
		void x;
		void y;
		void placePane();
	});

	async function _dispatchWrites(
		id: DeckId,
		targetBpm: number,
		pressT0Ms?: number
	): Promise<boolean> {
		const base = _baseBpm(id);
		if (base === null) return false;
		const writes = tempoWritesFromTargetBpm({
			deck: id,
			targetBpm,
			baseBpm: base,
			currentRange: pitchRanges[id]
		});
		if (writes === null) return false;
		for (const write of writes) {
			await runPerformanceCommandFromUi(write, pressT0Ms);
		}
		drafts[id] = _formatBpm(_effectiveBpm(id));
		return true;
	}

	async function commitDraft(id: DeckId, raw: string, pressT0Ms?: number): Promise<void> {
		const parsed = parseTempoBpmInput(raw);
		if (parsed === null) {
			drafts[id] = _formatBpm(_effectiveBpm(id));
			return;
		}
		const ok = await _dispatchWrites(id, parsed, pressT0Ms);
		if (!ok) drafts[id] = _formatBpm(_effectiveBpm(id));
	}

	function onBpmBlur(id: DeckId, event: FocusEvent): void {
		if (skipBlurCommit) return;
		void commitDraft(id, (event.currentTarget as HTMLInputElement).value);
	}

	function onBpmKeydown(id: DeckId, event: KeyboardEvent): void {
		const input = event.currentTarget as HTMLInputElement;
		if (event.key === 'Enter') {
			event.preventDefault();
			void commitDraft(id, input.value, event.timeStamp);
			return;
		}
		if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
			event.preventDefault();
			const delta = event.shiftKey ? 5 : 1;
			const sign = event.key === 'ArrowUp' ? 1 : -1;
			const parsed = parseTempoBpmInput(input.value);
			const base = parsed ?? _effectiveBpm(id);
			if (base === null) return;
			const target = nudgeBpm(base, sign * delta);
			void commitDraft(id, String(target), event.timeStamp);
		}
	}

	async function onHalve(id: DeckId, event: MouseEvent): Promise<void> {
		const effective = _effectiveBpm(id);
		if (effective === null) return;
		await _dispatchWrites(id, effective / 2, event.timeStamp);
	}

	async function onDouble(id: DeckId, event: MouseEvent): Promise<void> {
		const effective = _effectiveBpm(id);
		if (effective === null) return;
		await _dispatchWrites(id, effective * 2, event.timeStamp);
	}

	function onSliderInput(event: Event): void {
		const value = Number((event.currentTarget as HTMLInputElement).value);
		if (!Number.isFinite(value)) return;
		const ratio = pitchRatioFromFaderValue(value, pitchRanges[deckId]);
		void tempoDispatcher.request(ratio);
	}

	const sliderValue = $derived(
		faderValueFromPitchRatio(deckStates[deckId].pitch, pitchRanges[deckId])
	);
	const sliderPct = $derived(Math.round((deckStates[deckId].pitch - 1) * 100));

	function closePane(): void {
		skipBlurCommit = true;
		onclose();
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') {
			event.preventDefault();
			closePane();
		}
	}

	function onOutsidePointerDown(event: PointerEvent): void {
		if (pane !== null && event.target instanceof Node && !pane.contains(event.target)) {
			closePane();
		}
	}

	async function toggleMaster(id: DeckId): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'master', deck: id });
	}

	async function toggleBeatSync(id: DeckId): Promise<void> {
		const deck = deckStates[id];
		await runPerformanceCommandFromUi({
			type: 'beat_sync',
			deck: id,
			enabled: !deck.beat_sync_enabled
		});
	}

	async function toggleMasterTempo(id: DeckId): Promise<void> {
		const deck = deckStates[id];
		await runPerformanceCommandFromUi({
			type: 'master_tempo',
			deck: id,
			enabled: !deck.master_tempo_enabled
		});
	}

	async function toggleSyncMode(id: DeckId): Promise<void> {
		const deck = deckStates[id];
		const mode = deck.sync_mode === 'bar' ? 'beat' : 'bar';
		await runPerformanceCommandFromUi({ type: 'sync_mode', deck: id, mode });
	}

	async function nudgeKey(id: DeckId, semitones: -1 | 1): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'key_nudge', deck: id, semitones });
	}
</script>

<svelte:window onkeydown={onKeydown} onpointerdowncapture={onOutsidePointerDown} />

<div
	bind:this={pane}
	class="tempo-edit-modal"
	class:primary-deck={true}
	data-testid="tempo-edit-modal"
	role="dialog"
	aria-label={`tempo edit deck ${deckId}`}
	tabindex="-1"
	style={`left:${position.x}px;top:${position.y}px`}
>
	{#if masterBpm !== null}
		<div class="master-context" title="Effective BPM of the elected tempo MASTER deck">
			Master tempo: {masterBpm.toFixed(2)} BPM
		</div>
	{/if}
	{#each DECK_IDS as id (id)}
		{@const deck = deckStates[id]}
		{@const loaded = deck.stable_id !== null}
		{@const effective = _effectiveBpm(id)}
		{@const buttons = halveDoubleVisibility(effective ?? NaN)}
		{@const gridless = gridFeaturesInert(deck)}
		{@const keyPlayhead = keyAtPlayheadNow(
			deck.anlz,
			deck.position_ms,
			deck.key_shift_semitones,
			deck.key
		)}
		{@const keyText = keyPlayhead.display ?? '--'}
		{@const keyColor = camelotKeyColor(keyText === '--' ? null : keyText)}
		<div class="deck-row" class:primary={id === deckId} class:unloaded={!loaded}>
			<span class="deck-label">CH{id}</span>
			<input
				type="text"
				class="bpm-input"
				disabled={!loaded}
				value={drafts[id]}
				data-testid={`tempo-edit-bpm-deck-${id}`}
				aria-label={`tempo bpm deck ${id}`}
				title={loaded ? 'Effective playback BPM' : 'No track loaded'}
				oninput={(e) => (drafts[id] = e.currentTarget.value)}
				onblur={(e) => onBpmBlur(id, e)}
				onkeydown={(e) => onBpmKeydown(id, e)}
			/>
			{#if loaded && buttons.halve}
				<button
					type="button"
					class="halve-btn"
					data-testid={`tempo-edit-halve-deck-${id}`}
					aria-label={`halve tempo deck ${id}`}
					title="Halve effective BPM"
					onclick={(e) => void onHalve(id, e)}
				>
					Halve
				</button>
			{/if}
			{#if loaded && buttons.double}
				<button
					type="button"
					class="double-btn"
					data-testid={`tempo-edit-double-deck-${id}`}
					aria-label={`double tempo deck ${id}`}
					title="Double effective BPM"
					onclick={(e) => void onDouble(id, e)}
				>
					Double
				</button>
			{/if}
			{#if id === deckId && loaded}
				<!-- svelte-ignore a11y_no_redundant_roles -->
				<input
					type="range"
					min="0"
					max="1"
					step="0.001"
					class="tempo-slider"
					style="width: 200px"
					value={sliderValue}
					role="slider"
					aria-orientation="horizontal"
					aria-valuenow={sliderPct}
					aria-valuemin={-pitchRanges[id]}
					aria-valuemax={pitchRanges[id]}
					data-testid={`tempo-edit-slider-deck-${id}`}
					title={`Pitch ${sliderPct >= 0 ? '+' : ''}${sliderPct}%`}
					oninput={onSliderInput}
				/>
			{/if}
			<div class="context-controls">
				<button
					type="button"
					class="rb-lit-button"
					class:lit={deck.is_master}
					disabled={!loaded}
					aria-pressed={deck.is_master}
					data-testid={`tempo-edit-master-deck-${id}`}
					aria-label={`master deck ${id}`}
					title="MASTER"
					onclick={() => void toggleMaster(id)}
				>
					MASTER
				</button>
				<button
					type="button"
					class="rb-lit-button"
					class:lit={deck.beat_sync_enabled && !gridless}
					disabled={!loaded || gridless}
					aria-pressed={deck.beat_sync_enabled}
					data-testid={`tempo-edit-sync-deck-${id}`}
					aria-label={`beat sync deck ${id}`}
					title={gridless ? gridFeatureInertTip(deck) : 'Beat Sync'}
					onclick={() => void toggleBeatSync(id)}
				>
					BEAT SYNC
				</button>
				<button
					type="button"
					class="rb-lit-button phase-btn"
					class:lit={deck.sync_mode === 'bar'}
					disabled={!loaded}
					aria-pressed={deck.sync_mode === 'bar'}
					data-testid={`tempo-edit-phase-deck-${id}`}
					aria-label={`phase lock deck ${id}`}
					title="Phase lock (BEAT / BAR)"
					onclick={() => void toggleSyncMode(id)}
				>
					{deck.sync_mode === 'bar' ? 'BAR' : 'BEAT'}
				</button>
				<span class="key-group">
					<button
						type="button"
						class="nudge"
						disabled={!loaded || deck.key_shift_semitones === -12}
						aria-label={`lower key deck ${id}`}
						data-testid={`tempo-edit-key-down-deck-${id}`}
						onclick={() => void nudgeKey(id, -1)}
					>
						&lt;
					</button>
					<span class="key-val" style={keyColor !== null ? `color:${keyColor}` : undefined}>
						{keyText}
					</span>
					<button
						type="button"
						class="nudge"
						disabled={!loaded || deck.key_shift_semitones === 12}
						aria-label={`raise key deck ${id}`}
						data-testid={`tempo-edit-key-up-deck-${id}`}
						onclick={() => void nudgeKey(id, 1)}
					>
						&gt;
					</button>
				</span>
				<button
					type="button"
					class="rb-lit-button"
					class:lit={deck.master_tempo_enabled}
					disabled={!loaded}
					aria-pressed={deck.master_tempo_enabled}
					data-testid={`tempo-edit-mt-deck-${id}`}
					aria-label={`master tempo deck ${id}`}
					title="Master Tempo"
					onclick={() => void toggleMasterTempo(id)}
				>
					MT
				</button>
			</div>
		</div>
	{/each}
</div>

<style>
	.tempo-edit-modal {
		position: fixed;
		z-index: 1000;
		min-width: 280px;
		max-width: calc(100vw - 16px);
		max-height: calc(100vh - 16px);
		overflow: auto;
		padding: 8px;
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		background: var(--rb-panel-raised);
		box-shadow: 0 5px 18px rgb(0 0 0 / 45%);
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.master-context {
		font-size: 11px;
		color: var(--rb-text-dim);
		padding-bottom: 4px;
		border-bottom: 1px solid var(--rb-border);
	}
	.deck-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 4px;
		padding: 4px;
		border-radius: 3px;
	}
	.deck-row.primary {
		background: var(--rb-select);
	}
	.deck-row.unloaded {
		opacity: 0.5;
	}
	.deck-label {
		font-weight: 600;
		font-size: 11px;
		min-width: 28px;
	}
	.bpm-input {
		width: 64px;
		font: inherit;
		font-variant-numeric: tabular-nums;
		padding: 2px 4px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		background: var(--rb-panel);
		color: var(--rb-text);
	}
	.bpm-input:disabled {
		color: var(--rb-text-dim);
	}
	.halve-btn,
	.double-btn {
		font-size: 10px;
		padding: 2px 6px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		background: var(--rb-panel);
		color: var(--rb-text);
		cursor: pointer;
	}
	.halve-btn:hover,
	.double-btn:hover {
		background: var(--rb-select);
	}
	.tempo-slider {
		flex: 0 0 200px;
	}
	.context-controls {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 3px;
		width: 100%;
	}
	.context-controls .rb-lit-button {
		font-size: 9px;
		padding: 2px 4px;
	}
	.key-group {
		display: inline-flex;
		align-items: center;
		gap: 2px;
	}
	.key-group .nudge {
		font-size: 10px;
		padding: 0 4px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		background: var(--rb-panel);
		color: var(--rb-text);
		cursor: pointer;
	}
	.key-group .nudge:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}
	.key-val {
		font-size: 11px;
		min-width: 24px;
		text-align: center;
	}
</style>
