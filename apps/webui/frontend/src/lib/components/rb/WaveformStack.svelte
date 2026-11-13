<script lang="ts">
	// Build unit: wavestack (COMPONENT-MAP 1.2, SCREENSHOT-SPEC 2).
	// Four rows, one per deck 1-4, each a canvas scrolling window centered
	// on a fixed playhead. All per-row logic lives in wave/WaveRow.svelte;
	// painters and math in wave/render.ts + wave/wave-math.ts.
	import type { DeckId } from '$lib/rb/deck-slots';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import EdgeSamplerToggle from './sampler/EdgeSamplerToggle.svelte';
	import WaveRow from './wave/WaveRow.svelte';
	import { selectorNode } from '$lib/keyboard/selector-register';
	import { waveLaneSelectable, waveLaneSelectorId } from './waveform-library-selector-ids';

	// Always 4 entries, MORE or LESS - pin 862cd3's LESS mode collapses rows
	// 3/4 to 0px via CSS only (`.less` below); this array is never filtered,
	// so all four WaveRow instances stay mounted.
	const deckIds: DeckId[] = [1, 2, 3, 4];

	/** Pin 862cd3: MORE/LESS two-deck performance layout. */
	const deckLayoutLess = $derived(uiPrefs.deck_layout === 'less');
</script>

<div class="rb-wavestack" class:less={deckLayoutLess}>
	{#each deckIds as deckId (deckId)}
		<!-- #13703: each lane is a selector leaf; the wrapper stays out of the grid. -->
		<div
			class="wave-lane-node"
			use:selectorNode={{
				id: waveLaneSelectorId(deckId),
				kind: 'leaf',
				enabled: waveLaneSelectable(deckId, uiPrefs.deck_layout)
			}}
		>
			<WaveRow {deckId} />
		</div>
	{/each}
	<!-- #13722: the edge sampler on/off chip, beside deck 1's ZOOM chip. -->
	<div class="sampler-toggle-slot" data-testid="edge-sampler-toggle-slot">
		<EdgeSamplerToggle />
	</div>
</div>

<style>
	.wave-lane-node {
		display: contents;
	}
	.rb-wavestack {
		grid-area: wavestack;
		position: relative;
		display: grid;
		grid-template-rows: repeat(4, minmax(var(--rb-waverow-h), auto));
		border-bottom: 1px solid var(--rb-border);
		min-width: 0;
		transition: grid-template-rows var(--rb-deck-layout-duration, 200ms) ease;
	}
	/* The mock's `.samp-toggle` spot: just under the top bar, beside the waveform
	 * ZOOM chip (WaveRow's `.zoom-chip`, top 2px right 4px of deck 1's row, about
	 * 57px wide at "16 BARS"). Absolute, so it takes no grid row. */
	.sampler-toggle-slot {
		position: absolute;
		top: 2px;
		right: 66px;
		z-index: 4;
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

	/* A11Y-11811: reduced motion (OS or Appearance); mirrors app.css. */
	@media (prefers-reduced-motion: reduce) {
		:where(*) {
			animation-duration: 0.01ms;
			animation-iteration-count: 1;
			transition-duration: 0.01ms;
		}
	}
	:global(html[data-reduce-motion]) :where(*) {
		animation-duration: 0.01ms;
		animation-iteration-count: 1;
		transition-duration: 0.01ms;
	}
</style>
