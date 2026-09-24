<script lang="ts">
	// Build unit: wavestack (COMPONENT-MAP 1.2, SCREENSHOT-SPEC 2).
	// Four rows, one per deck 1-4, each a canvas scrolling window centered
	// on a fixed playhead. All per-row logic lives in wave/WaveRow.svelte;
	// painters and math in wave/render.ts + wave/wave-math.ts.
	import type { DeckId } from '$lib/rb/deck-slots';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import WaveRow from './wave/WaveRow.svelte';

	// Always 4 entries, MORE or LESS - pin 862cd3's LESS mode collapses rows
	// 3/4 to 0px via CSS only (`.less` below); this array is never filtered,
	// so all four WaveRow instances stay mounted.
	const deckIds: DeckId[] = [1, 2, 3, 4];

	/** Pin 862cd3: MORE/LESS two-deck performance layout. */
	const deckLayoutLess = $derived(uiPrefs.deck_layout === 'less');
</script>

<div class="rb-wavestack" class:less={deckLayoutLess}>
	{#each deckIds as deckId (deckId)}
		<WaveRow {deckId} />
	{/each}
</div>

<style>
	.rb-wavestack {
		grid-area: wavestack;
		display: grid;
		grid-template-rows: repeat(4, minmax(var(--rb-waverow-h), auto));
		border-bottom: 1px solid var(--rb-border);
		min-width: 0;
		transition: grid-template-rows var(--rb-deck-layout-duration, 200ms) ease;
	}
	/* LESS: rows 3/4 (decks 3/4) collapse to 0 INSIDE this component's own
	 * grid, but that alone does not free height to the deckarea/library
	 * rows below - +page.svelte's outer `.perf-root.deck-layout-less`
	 * grid-template-rows independently shrinks the wavestack area's OWN
	 * track to 2 waverows (not 4) so the two grids agree and the library's
	 * `1fr` row actually picks up the difference. Rows 1/2 (decks 1/2) are
	 * untouched. */
	.rb-wavestack.less {
		grid-template-rows: minmax(var(--rb-waverow-h), auto) minmax(var(--rb-waverow-h), auto) 0px 0px;
	}
	.rb-wavestack.less :global([data-deck='3']),
	.rb-wavestack.less :global([data-deck='4']) {
		opacity: 0;
		pointer-events: none;
		transition: opacity var(--rb-deck-layout-duration, 200ms) ease;
	}
</style>
