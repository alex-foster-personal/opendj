<script lang="ts">
	// Beat jump: -8 / -4 / +4 / +8 whole-beat transport jumps.
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
	import type { AnlzBeat } from '$lib/rb/anlz-types';
	import type { DeckState } from '$lib/rb/deck-state-types';

	let {
		deck,
		pending,
		onJump
	}: {
		deck: DeckState;
		pending: boolean;
		onJump: (beats: number) => Promise<void>;
	} = $props();

	const JUMPS: readonly number[] = [-8, -4, 4, 8];

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
		return delta > 0 ? `+${delta}` : String(delta);
	}

	function _canJump(delta: number): boolean {
		if (blockedReason !== null) return false;
		// Without a known duration, the engine's own duration clamp
		// (beatJumpTargetWithinDurationMs) cannot be mirrored here, so treat
		// jumping as unavailable rather than risk enabling a button that
		// silently no-ops once the engine clamps its target.
		if (deck.duration_ms === null) return false;
		return beatJumpMovesTransportWithinDuration(beats, deck.position_ms, delta, deck.duration_ms);
	}

	function _title(delta: number): string {
		if (blockedReason !== null) return blockedReason;
		if (!_canJump(delta)) {
			return delta < 0
				? `already within ${-delta} beats of the first beatgrid beat`
				: `fewer than ${delta} beatgrid beats remain ahead`;
		}
		return `jump ${_label(delta)} beats along the real PQTZ beatgrid`;
	}

	async function jump(delta: number): Promise<void> {
		if (!_canJump(delta)) return;
		await onJump(delta);
	}
</script>

<div class="beat-jump" title="Beat jump: move the transport by whole beatgrid beats">
	{#each JUMPS as delta (delta)}
		<button
			disabled={!_canJump(delta)}
			data-performance-control="beat-jump"
			data-beats={delta}
			title={_title(delta)}
			onclick={() => void jump(delta)}
		>
			{_label(delta)}
		</button>
	{/each}
</div>

<style>
	/* A single row of 4, not a 2x2 grid: the loop column only budgets one
	 * row's worth of spare height below LoopCluster (fixed deck height,
	 * clipped by main-row's overflow: hidden), so a second row is clipped.
	 * Widening trades width for height; Deck.svelte's cue-flex is sized to
	 * absorb exactly this kind of width change (see its own comment). */
	.beat-jump {
		display: grid;
		grid-template-columns: repeat(4, minmax(20px, 1fr));
		gap: 2px;
		flex: 0 0 auto;
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
