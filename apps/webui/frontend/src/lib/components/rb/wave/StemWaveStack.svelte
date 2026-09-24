<script lang="ts">
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { STEM_CONTROLS } from '$lib/rb/stem-graph';
	import StemWaveRow from './StemWaveRow.svelte';

	let {
		deck,
		showStems,
		scrollPx,
		canvasWidth
	}: {
		deck: DeckState;
		showStems: boolean;
		scrollPx: number;
		canvasWidth: number;
	} = $props();
</script>

{#if showStems && deck.stable_id !== null}
	<div class="stem-wave-stack" data-testid={`stem-wave-stack-deck-${deck.deck_id}`}>
		{#each STEM_CONTROLS as stem (stem)}
			<StemWaveRow {deck} {stem} {showStems} {scrollPx} {canvasWidth} />
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
