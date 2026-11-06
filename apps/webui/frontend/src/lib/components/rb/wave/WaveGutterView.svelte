<script lang="ts">
	// Props-only half of WaveGutter (DECKUX-40): a large deck number in the
	// brand face, then the track summary, which WaveGutter.svelte passes in as
	// `summary` because the summary reads the live key and artwork. An empty
	// deck shows only its number.
	import type { Snippet } from 'svelte';
	import type { DeckState } from '$lib/rb/deck-state-types';

	const {
		deckId,
		loaded,
		summary
	}: {
		deckId: DeckState['deck_id'];
		/** A track is on the deck; false shows the number alone, dimmed. */
		loaded: boolean;
		summary: Snippet;
	} = $props();

	const deckNumTitle = $derived(
		loaded ? `Waveform row for deck ${deckId}` : `Waveform row for deck ${deckId}: no track loaded`
	);
</script>

<div class="gutter">
	<span class="deck-num" class:empty={!loaded} title={deckNumTitle}>{deckId}</span>
	{#if loaded}{@render summary()}{/if}
</div>

<style>
	.gutter {
		width: 160px;
		flex: none;
		display: flex;
		align-items: stretch;
		gap: 6px;
		padding: 0 6px 0 4px;
		border-right: 1px solid var(--rb-border);
		box-sizing: border-box;
	}
	.deck-num {
		width: 24px;
		flex: none;
		align-self: center;
		text-align: center;
		color: var(--rb-text);
		font-family: var(--rb-font-brand);
		font-weight: 800;
		font-stretch: 150%;
		font-size: calc(var(--rb-waverow-h) * 0.5);
		line-height: 1;
	}
	/* "Empty state should be less messy - just show nothing": the number stays
	   so the row is still identifiable, at the border token's weight. */
	.deck-num.empty {
		color: var(--rb-border);
	}
</style>
