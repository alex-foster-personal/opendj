<script lang="ts">
	/**
	 * ARTNUM-9649b: the deck number as a white outline with a dark halo, centered
	 * over a deck's artwork square. Props in, nothing global: the parent
	 * passes the pref and the square's size. Static CSS, no per-frame work.
	 * The parent must be position: relative.
	 */
	import { deckNumberArtVisible, type DeckNumberArt } from '$lib/rb/deck-number-art';

	let { deck, mode, sizePx }: { deck: number; mode: DeckNumberArt; sizePx: number } = $props();
</script>

{#if deckNumberArtVisible(mode, sizePx)}
	<span
		class="deck-num-art"
		style:--art-s="{sizePx}px"
		aria-hidden="true"
		data-testid="artwork-deck-number">{deck}</span
	>
{/if}

<style>
	.deck-num-art {
		position: absolute;
		inset: 0;
		display: grid;
		place-items: center;
		pointer-events: none;
		font-family: var(--rb-font-brand);
		font-weight: 800;
		font-stretch: 150%;
		font-size: calc(var(--art-s) * 0.5);
		line-height: 1;
		color: transparent;
		-webkit-text-stroke: max(1px, calc(var(--art-s) * 0.035)) #fff;
		/* Halo: mimicked from Material's text-on-image scrim guidance. */
		filter: drop-shadow(0 0 calc(var(--art-s) * 0.06) rgb(0 0 0 / 0.85))
			drop-shadow(0 0 calc(var(--art-s) * 0.12) rgb(0 0 0 / 0.6));
	}
</style>
