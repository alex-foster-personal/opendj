<script lang="ts">
	import EnergyStrip from './EnergyStrip.svelte';
	import { energyBlock, energyLineSegments } from './tryer-model';
	import type { TryCandidate } from './tryer-model';

	let { chip, styleVisible }: { chip: TryCandidate; styleVisible: boolean } = $props();

	const energy = $derived(energyBlock(chip));
	const segments = $derived(energyLineSegments(energy));
</script>

{#if styleVisible}
	<p class="energy" data-testid="tryer-energy-line">
		{#each segments as segment (segment.text + (segment.title ?? ''))}
			{#if segment.title !== null}
				<span title={segment.title}>{segment.text}</span>
			{:else}
				{segment.text}
			{/if}
		{/each}
	</p>
	<EnergyStrip block={energy} />
{/if}

<style>
	.energy {
		margin: 0.25rem 0 0;
		font-size: 0.78em;
		line-height: 1.35;
		opacity: 0.9;
	}
	.energy span {
		cursor: help;
		text-decoration: underline dotted rgba(255, 255, 255, 0.35);
		text-underline-offset: 0.15em;
	}
</style>
