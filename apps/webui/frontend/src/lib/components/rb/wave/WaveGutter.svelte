<script lang="ts">
	// Stable waveform-row chrome. Kept separate from canvas interaction so the
	// identity column can grow without making the hot render component a god file.
	import type { DeckState } from '$lib/rb/deck-state-types';
	import WaveTrackSummary from './WaveTrackSummary.svelte';

	const {
		deck,
		deckId,
		barsLabel
	}: {
		deck: DeckState;
		deckId: DeckState['deck_id'];
		barsLabel: string | null;
	} = $props();

	const deckNumTitle = `Waveform row for deck ${deckId}`;
	const barsTitle =
		'Whole bars until the next memory cue, hot cue, phrase, or end of the beatgrid';
</script>

<div class="gutter">
	<div class="deck-indicators">
		<span class="deck-num" title={deckNumTitle}>{deckId}</span>
		{#if barsLabel !== null}<span class="bars" title={barsTitle}>{barsLabel}</span>{/if}
	</div>
	<WaveTrackSummary {deck} />
</div>

<style>
	.gutter {
		width: 132px;
		flex: none;
		display: flex;
		align-items: stretch;
		gap: 4px;
		padding: 2px 4px 2px 6px;
		border-right: 1px solid var(--rb-border);
	}
	.deck-indicators {
		width: 25px;
		flex: none;
		display: flex;
		flex-direction: column;
		justify-content: center;
		gap: 1px;
	}
	.deck-num {
		color: var(--rb-text);
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
		line-height: 1.1;
	}
	.bars {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		white-space: nowrap;
	}
</style>
