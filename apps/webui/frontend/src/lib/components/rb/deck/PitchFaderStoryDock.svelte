<script lang="ts">
	/** Storybook-only: feeds tempo/range callbacks back into props like the deck engine. */
	import type { PitchRange } from '$lib/player/constants';
	import type { DeckState } from '$lib/rb/deck-state-types';

	import PitchFader from './PitchFader.svelte';

	let {
		deck,
		pitchRange,
		pending,
		onTempoChange,
		onRangeChange
	}: {
		deck: DeckState;
		pitchRange: PitchRange;
		pending: boolean;
		onTempoChange: (ratio: number) => Promise<void>;
		onRangeChange: (range: PitchRange) => Promise<void>;
	} = $props();

	let pitch: number | null = $state(null);
	let range: PitchRange | null = $state(null);
	const shown = $derived({ ...deck, pitch: pitch ?? deck.pitch });
</script>

<div class="dock">
	<PitchFader
		deck={shown}
		pitchRange={range ?? pitchRange}
		{pending}
		onTempoChange={async (r) => {
			pitch = r;
			await onTempoChange(r);
		}}
		onRangeChange={async (r) => {
			range = r;
			await onRangeChange(r);
		}}
	/>
</div>

<style>
	.dock {
		height: 160px;
		display: flex;
	}
</style>
