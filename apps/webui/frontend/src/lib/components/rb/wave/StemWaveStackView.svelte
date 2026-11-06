<script lang="ts">
	// Props-only half of StemWaveStack: the column of per-stem strips under a
	// deck waveform. StemWaveStack.svelte passes each strip in as `row`, since
	// a strip fetches its own envelope.
	import type { Snippet } from 'svelte';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { STEM_CONTROLS } from '$lib/rb/stem-graph';
	import type { StemControl } from '$lib/rb/stem-types';

	let {
		deckId,
		visible,
		row
	}: {
		deckId: DeckState['deck_id'];
		/** Stems shown AND a track loaded; false renders nothing. */
		visible: boolean;
		row: Snippet<[StemControl]>;
	} = $props();
</script>

{#if visible}
	<div class="stem-wave-stack" data-testid={`stem-wave-stack-deck-${deckId}`}>
		{#each STEM_CONTROLS as stem (stem)}
			{@render row(stem)}
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
