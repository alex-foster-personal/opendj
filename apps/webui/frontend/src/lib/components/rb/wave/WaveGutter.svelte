<script lang="ts">
	// Stable waveform-row chrome. Kept separate from canvas interaction so the
	// identity column can grow without making the hot render component a god file.
	// DECKUX-40 (the maintainer, Tue 6 Oct 2026): a large deck number in the brand face,
	// then the track summary. An empty deck shows only its number, and there is
	// no bars countdown ("not clear to me what we'd use it for").
	import type { DeckState } from '$lib/rb/deck-state-types';
	import WaveTrackSummary from './WaveTrackSummary.svelte';

	const {
		deck,
		deckId
	}: {
		deck: DeckState;
		deckId: DeckState['deck_id'];
	} = $props();

	const loaded = $derived(deck.stable_id !== null);
	const deckNumTitle = $derived(
		loaded ? `Waveform row for deck ${deckId}` : `Waveform row for deck ${deckId}: no track loaded`
	);
</script>

<div class="gutter">
	<span class="deck-num" class:empty={!loaded} title={deckNumTitle}>{deckId}</span>
	{#if loaded}<WaveTrackSummary {deck} />{/if}
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
