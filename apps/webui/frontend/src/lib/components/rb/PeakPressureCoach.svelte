<script lang="ts">
	import ControlExplainer from './deck/ControlExplainer.svelte';

	let {
		cue,
		cueLabel
	}: {
		cue: 'keep_building' | 'hold' | 'release' | 'unknown';
		cueLabel: string;
	} = $props();
</script>

{#if cue !== 'unknown' && cueLabel}
	<ControlExplainer
		title="Peak energy pressure (educational)"
		bullets={[
			'Educational coach from track metadata, not the room.',
			'Metadata energy is not crowd response.',
			'Advisory only. Your ears and the room win.'
		]}
	>
		<span
			class="coach"
			data-testid="peak-pressure-coach"
			data-cue={cue}
			role="status"
			aria-live="polite"
		>
			{cueLabel}
		</span>
	</ControlExplainer>
{/if}

<style>
	.coach {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-browser);
	}
</style>
