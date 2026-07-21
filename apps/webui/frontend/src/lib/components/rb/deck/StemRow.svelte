<script lang="ts">
	// Stem row (SCREENSHOT-SPEC 3): MUTE label + VOCAL / INST / DRUMS chips.
	// Explicitly INERT v1 (no stem separation backing, SCREENSHOT-SPEC 7);
	// outline colours are authentic: VOCAL green, INST orange, DRUMS blue.
	let { inertTip }: { inertTip: string } = $props();

	const STEMS: { label: string; colorVar: string }[] = [
		{ label: 'VOCAL', colorVar: 'var(--rb-green)' },
		{ label: 'INST', colorVar: 'var(--rb-orange)' },
		{ label: 'DRUMS', colorVar: 'var(--rb-accent)' }
	];
</script>

<div class="stems">
	<span class="mute">MUTE</span>
	{#each STEMS as stem (stem.label)}
		<button class="chip rb-inert" disabled title={inertTip} style={`--chip-color:${stem.colorVar}`}>
			{stem.label}
		</button>
	{/each}
</div>

<style>
	.stems {
		display: flex;
		align-items: center;
		gap: 4px;
		flex: 0 0 auto;
	}
	.mute {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		font-weight: 600;
		margin-right: 2px;
	}
	.chip {
		background: transparent;
		border: 1px solid var(--chip-color);
		border-radius: 2px;
		color: var(--chip-color);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		padding: 1px 8px;
		cursor: default;
	}
</style>
