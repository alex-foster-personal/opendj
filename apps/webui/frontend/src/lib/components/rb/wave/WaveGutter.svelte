<script lang="ts">
	// Stable waveform-row chrome. Kept separate from canvas interaction so the
	// identity column can grow without making the hot render component a god file.
	// DECKUX-40 (the maintainer, Tue 6 Oct 2026): a large deck number in the brand face,
	// then the track summary. An empty deck shows only its number, and there is
	// no bars countdown ("not clear to me what we'd use it for"). The markup
	// lives in WaveGutterView.svelte so it can carry a story.
	import type { DeckState } from '$lib/rb/deck-state-types';
	import WaveGutterView from './WaveGutterView.svelte';
	import WaveTrackSummary from './WaveTrackSummary.svelte';

	const {
		deck,
		deckId
	}: {
		deck: DeckState;
		deckId: DeckState['deck_id'];
	} = $props();

	const loaded = $derived(deck.stable_id !== null);
</script>

<WaveGutterView {deckId} {loaded}>
	{#snippet summary()}<WaveTrackSummary {deck} />{/snippet}
</WaveGutterView>
