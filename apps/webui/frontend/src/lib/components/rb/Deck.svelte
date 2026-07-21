<script lang="ts">
	// Build unit: deck (COMPONENT-MAP 1.3). Stub - owner replaces the body.
	// Real elements: header meta (TrackOut), artwork (/artwork?size=m),
	// overview strip + hot-cue bank (/anlz), CUE + play/pause + hot-cue jump
	// (audio engine). Inert: KEY SYNC, BEAT SYNC, Q, SLIP, MT, AU/MA, stems,
	// loop cluster, pitch - tooltip 'not implemented - see PARITY-TODO'.
	import type { DeckId } from '$lib/rb/types';

	let { deckId }: { deckId: DeckId } = $props();
</script>

<section class="rb-deck rb-panel" data-deck={deckId}>
	<span class="title">Deck {deckId}</span>
	<span class="note">stub - build unit: deck</span>
</section>

<style>
	.rb-deck {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 4px;
		padding: 8px;
		min-height: 0;
		flex: 1;
	}
	.title {
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
	}
	.note {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
	}
</style>
