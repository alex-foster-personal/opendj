<script lang="ts">
	// Build unit: wavestack (COMPONENT-MAP 1.2, SCREENSHOT-SPEC 2).
	// Four rows, one per deck 1-4, each a canvas scrolling window centered
	// on a fixed playhead. All per-row logic lives in wave/WaveRow.svelte;
	// painters and math in wave/render.ts + wave/wave-math.ts.
	import type { DeckId } from '$lib/rb/deck-slots';
	import WaveRow from './wave/WaveRow.svelte';

	const deckIds: DeckId[] = [1, 2, 3, 4];
</script>

<div class="rb-wavestack">
	{#each deckIds as deckId (deckId)}
		<WaveRow {deckId} />
	{/each}
</div>

<style>
	.rb-wavestack {
		grid-area: wavestack;
		display: grid;
		grid-template-rows: repeat(4, var(--rb-waverow-h));
		border-bottom: 1px solid var(--rb-border);
		min-width: 0;
	}
</style>
