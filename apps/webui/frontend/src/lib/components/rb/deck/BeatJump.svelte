<script lang="ts">
	// Beat jump (DECKUX-10470, rekordbox / CDJ-3000 BEAT JUMP, mimicked): one
	// per-deck size (1/2 to 64 beats) stepped with < >, and two buttons that
	// jump back or forward by it. A half beat lands on a grid interval midpoint.
	//
	// REAL behaviour only. Each button counts exact PQTZ grid beats through
	// the shared typed dispatcher (`beat_jump`), never a BPM-derived offset,
	// so a track whose tempo drifts still lands on a real beat. With no
	// loaded track, no beatgrid, or no grid left in the jump direction, the
	// button renders inert with a title saying which of those it is.
	//
	// Sized to fit the spare height already present in the loop column - the
	// deck bounding box must not grow to host it.
	import { beatJumpMovesTransportWithinDuration } from '$lib/rb/beat-sync-math';
	import { shiftLiveBeatLoopRangeMs } from '$lib/player/transport/loops';
	import type { AnlzBeat } from '$lib/rb/anlz-types';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import {
		beatJumpSizeLabel,
		beatJumpSizes,
		steppedBeatJumpSize,
		type BeatJumpSize
	} from '$lib/rb/beat-jump-size.svelte';

	let {
		deck,
		pending,
		onJump,
		onSize
	}: {
		deck: DeckState;
		pending: boolean;
		onJump: (beats: number) => Promise<void>;
		onSize: (beats: BeatJumpSize) => Promise<void>;
	} = $props();

	const size: BeatJumpSize = $derived(beatJumpSizes[deck.deck_id]);
	const smaller: BeatJumpSize = $derived(steppedBeatJumpSize(size, -1));
	const larger: BeatJumpSize = $derived(steppedBeatJumpSize(size, 1));

	const beats: readonly AnlzBeat[] = $derived(deck.anlz?.beatgrid.beats ?? []);
	const blockedReason: string | null = $derived(
		pending
			? 'deck command pending'
			: deck.stable_id === null
				? 'no track loaded'
				: beats.length < 2
					? 'track has no usable beatgrid - beat jump unavailable'
					: null
	);

	// ----------------------------------------------------------- _helpers

	function _label(delta: number): string {
		const magnitude = beatJumpSizeLabel(Math.abs(delta));
		return delta > 0 ? `+${magnitude}` : `-${magnitude}`;
	}

	function _canJump(delta: number): boolean {
		if (blockedReason !== null) return false;
		// Without a known duration, the engine's own duration clamp
		// (beatJumpTargetWithinDurationMs) cannot be mirrored here, so treat
		// jumping as unavailable rather than risk enabling a button that
		// silently no-ops once the engine clamps its target.
		if (deck.duration_ms === null) return false;
		if (!beatJumpMovesTransportWithinDuration(beats, deck.position_ms, delta, deck.duration_ms)) {
			return false;
		}
		if (deck.loop !== null && deck.loop.engaged) {
			try {
				shiftLiveBeatLoopRangeMs(beats, deck.loop, delta, deck.duration_ms);
			} catch {
				return false;
			}
		}
		return true;
	}

	function _title(delta: number): string {
		if (blockedReason !== null) return blockedReason;
		if (!_canJump(delta)) {
			if (deck.loop !== null && deck.loop.engaged && !Number.isInteger(delta)) {
				return 'a live loop shifts by whole beats only - pick 1 beat or more';
			}
			if (deck.loop !== null && deck.loop.engaged) {
				return `the live loop cannot shift ${_label(delta)} beats without leaving the real PQTZ grid`;
			}
			return delta < 0
				? `already within ${beatJumpSizeLabel(-delta)} beats of the first beatgrid beat`
				: `fewer than ${beatJumpSizeLabel(delta)} beatgrid beats remain ahead`;
		}
		return `jump ${_label(delta)} beats along the real PQTZ beatgrid`;
	}

	async function jump(delta: number): Promise<void> {
		if (!_canJump(delta)) return;
		await onJump(delta);
	}

	async function step(next: BeatJumpSize): Promise<void> {
		if (pending || next === size) return;
		await onSize(next);
	}
</script>

<div
	class="beat-jump"
	role="group"
	aria-label={`beat jump deck ${deck.deck_id}`}
	title="Beat jump: move the transport by the chosen number of beatgrid beats"
>
	<span class="column-label">JUMP</span>
	<button
		class="size-step"
		disabled={pending || smaller === size}
		data-performance-control="beat-jump-size-down"
		data-testid={`beat-jump-size-down-deck-${deck.deck_id}`}
		aria-label={`smaller beat jump size deck ${deck.deck_id}`}
		title={smaller === size
			? 'smallest jump size'
			: `jump size ${beatJumpSizeLabel(smaller)} beats`}
		onclick={() => void step(smaller)}
		><svg viewBox="0 0 8 8" width="7" height="7" aria-hidden="true" focusable="false"
			><path
				d="M5.2 1.4 2.6 4 5.2 6.6"
				fill="none"
				stroke="currentColor"
				stroke-width="1.4"
				stroke-linecap="round"
				stroke-linejoin="round"
			/></svg
		></button
	>
	<span class="size-readout" data-testid={`beat-jump-size-deck-${deck.deck_id}`}
		>{beatJumpSizeLabel(size)}</span
	>
	<button
		class="size-step"
		disabled={pending || larger === size}
		data-performance-control="beat-jump-size-up"
		data-testid={`beat-jump-size-up-deck-${deck.deck_id}`}
		aria-label={`larger beat jump size deck ${deck.deck_id}`}
		title={larger === size ? 'largest jump size' : `jump size ${beatJumpSizeLabel(larger)} beats`}
		onclick={() => void step(larger)}
		><svg viewBox="0 0 8 8" width="7" height="7" aria-hidden="true" focusable="false"
			><path
				d="M2.8 1.4 5.4 4 2.8 6.6"
				fill="none"
				stroke="currentColor"
				stroke-width="1.4"
				stroke-linecap="round"
				stroke-linejoin="round"
			/></svg
		></button
	>
	{#each [-size, size] as delta (delta)}
		<button
			class="jump"
			disabled={!_canJump(delta)}
			data-performance-control="beat-jump"
			data-testid={`beat-jump-${delta < 0 ? 'back' : 'forward'}-deck-${deck.deck_id}`}
			aria-label={`jump ${delta < 0 ? 'back' : 'forward'} ${beatJumpSizeLabel(size)} beats deck ${deck.deck_id}`}
			data-beats={delta}
			title={_title(delta)}
			onclick={() => void jump(delta)}
		>
			{#if delta < 0}
				<svg viewBox="0 0 8 8" width="7" height="7" aria-hidden="true" focusable="false"
					><path d="M6 1.2 2.4 4 6 6.8Z" fill="currentColor" /></svg
				>
			{:else}
				<svg viewBox="0 0 8 8" width="7" height="7" aria-hidden="true" focusable="false"
					><path d="M2 1.2 5.6 4 2 6.8Z" fill="currentColor" /></svg
				>
			{/if}
		</button>
	{/each}
</div>

<style>
	/* Two compact columns sit to the right of LoopCluster in loop-col without
	 * borrowing any hot-cue-bank space. The local group is horizontal, so the
	 * grid's second row uses the loop cluster's existing vertical footprint. */
	.beat-jump {
		display: grid;
		grid-template-columns: repeat(6, minmax(0, 1fr));
		gap: 2px;
		width: 38px;
		flex: 0 0 auto;
	}
	.column-label {
		grid-column: 1 / -1;
		color: var(--rb-text-dim);
		font-size: 7px;
		font-weight: 700;
		line-height: 7px;
		letter-spacing: 0.45px;
		text-align: center;
	}
	.size-step {
		grid-column: span 2;
	}
	.size-readout {
		grid-column: span 2;
		color: var(--rb-text-dim);
		font-size: 8px;
		font-variant-numeric: tabular-nums;
		line-height: 13px;
		text-align: center;
		white-space: nowrap;
	}
	.jump {
		grid-column: span 3;
	}
	.beat-jump button svg {
		display: block;
		margin: 0 auto;
	}
	.beat-jump button {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 9px;
		font-variant-numeric: tabular-nums;
		line-height: 1;
		padding: 2px 0;
		cursor: pointer;
	}
	.beat-jump button:hover:not(:disabled) {
		border-color: var(--rb-accent);
		color: var(--rb-accent);
	}
	.beat-jump button:disabled {
		color: var(--rb-text-dim);
		cursor: default;
		opacity: 0.6;
	}
</style>
