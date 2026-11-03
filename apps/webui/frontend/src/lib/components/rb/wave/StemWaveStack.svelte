<script lang="ts">
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { STEM_CONTROLS } from '$lib/rb/stem-graph';
	import StemWaveRow from './StemWaveRow.svelte';

	let {
		deck,
		showStems,
		scrollPx,
		canvasWidth,
		windowS,
		playheadFrac
	}: {
		deck: DeckState;
		showStems: boolean;
		scrollPx: number;
		canvasWidth: number;
		/** The main row's zoom window and playhead place (WAVEZOOM-6802/05). */
		windowS: number;
		playheadFrac: number;
	} = $props();
</script>

{#if showStems && deck.stable_id !== null}
	<div class="stem-wave-stack" data-testid={`stem-wave-stack-deck-${deck.deck_id}`}>
		{#each STEM_CONTROLS as stem (stem)}
			<StemWaveRow {deck} {stem} {showStems} {scrollPx} {canvasWidth} {windowS} {playheadFrac} />
		{/each}
	</div>
{/if}

<style>
	.stem-wave-stack {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-width: 0;
	}
</style>
