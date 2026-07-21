<script lang="ts">
	/**
	 * Crossfader assign matrix: one 2x2 numeral grid (1 2 / 3 4) per bus
	 * (SCREENSHOT-SPEC 4). Real control: clicking a numeral routes that
	 * channel to this matrix's bus; clicking again releases it to THRU.
	 * Pure gain-math routing in the audio engine, no backend.
	 */
	import type { CrossfaderAssign, DeckId } from '$lib/rb/types';

	interface Props {
		/** Which crossfader bus this matrix assigns to (A = left, B = right). */
		bus: 'A' | 'B';
		/** Current assignment per channel (shared across both matrices). */
		assigns: Record<DeckId, CrossfaderAssign>;
		/** Called with the channel's new assignment on click. */
		onassign: (deck: DeckId, assign: CrossfaderAssign) => void;
	}

	let { bus, assigns, onassign }: Props = $props();

	// Cell order matches the screenshot grids: 1 2 on top, 3 4 below.
	const CELLS: DeckId[] = [1, 2, 3, 4];

	function handleClick(deck: DeckId): void {
		const next: CrossfaderAssign = assigns[deck] === bus ? 'THRU' : bus;
		onassign(deck, next);
	}
</script>

<div class="matrix" aria-label={`crossfader ${bus} assign matrix`}>
	{#each CELLS as deck (deck)}
		<button
			class="cell"
			class:lit={assigns[deck] === bus}
			title={`assign channel ${deck} to crossfader ${bus === 'A' ? 'left (A)' : 'right (B)'} bus`}
			onclick={() => handleClick(deck)}
		>
			{deck}
		</button>
	{/each}
</div>

<style>
	.matrix {
		display: grid;
		grid-template-columns: repeat(2, 15px);
		grid-auto-rows: 13px;
		gap: 2px;
	}
	.cell {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 9px;
		line-height: 1;
		padding: 0;
		cursor: pointer;
	}
	.cell.lit {
		color: #fff;
		background: var(--rb-accent);
		box-shadow: 0 0 4px var(--rb-accent-glow);
	}
</style>
