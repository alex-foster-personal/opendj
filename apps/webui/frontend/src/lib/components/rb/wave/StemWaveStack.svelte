<script lang="ts">
	import type { DeckState } from '$lib/rb/deck-state-types';
	import StemWaveRow from './StemWaveRow.svelte';
	import StemWaveStackView from './StemWaveStackView.svelte';
	import type { StemWaveRowCommand } from './stem-waveform-ui';

	let {
		deck,
		showStems,
		scrollPx,
		canvasWidth,
		windowS,
		playheadFrac,
		onSolo,
		onMute
	}: {
		deck: DeckState;
		showStems: boolean;
		scrollPx: number;
		canvasWidth: number;
		/** The main row's zoom window and playhead place (WAVEZOOM-6802/05). */
		windowS: number;
		playheadFrac: number;
		onSolo?: ((command: StemWaveRowCommand, pressT0Ms: number) => void) | undefined;
		onMute?: ((command: StemWaveRowCommand, pressT0Ms: number) => void) | undefined;
	} = $props();
</script>

<StemWaveStackView deckId={deck.deck_id} visible={showStems && deck.stable_id !== null}>
	{#snippet row(stem)}
		<StemWaveRow
			{deck}
			{stem}
			{showStems}
			{scrollPx}
			{canvasWidth}
			{windowS}
			{playheadFrac}
			{onSolo}
			{onMute}
		/>
	{/snippet}
</StemWaveStackView>
