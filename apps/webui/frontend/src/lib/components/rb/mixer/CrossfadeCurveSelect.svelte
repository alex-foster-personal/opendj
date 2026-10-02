<script lang="ts">
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import { setCrossfadeCurve, uiPrefs, type CrossfadeCurve } from '$lib/rb/prefs.svelte';

	const OPTIONS: { value: CrossfadeCurve; label: string; disabled?: boolean }[] = [
		{ value: 'magic', label: 'magic crossfader' },
		{ value: 'bass_swap', label: 'bass swap (tbd)', disabled: true },
		{ value: 'linear', label: 'linear (tbd)', disabled: true }
	];

	const selectedLabel = $derived(
		OPTIONS.find((option) => option.value === uiPrefs.crossfade_curve)?.label ?? 'magic crossfader'
	);

	function handleChange(event: Event): void {
		const value = (event.currentTarget as HTMLSelectElement).value as CrossfadeCurve;
		if (value === 'magic') setCrossfadeCurve(value);
	}
</script>

<select
	class="xf-curve"
	aria-label="crossfade curve"
	title={selectedLabel}
	value={uiPrefs.crossfade_curve}
	onchange={handleChange}
>
	{#each OPTIONS as option (option.value)}
		<option value={option.value} disabled={option.disabled} title={option.disabled ? `${option.label}: ${plannedTitle('crossfade-curve')}` : option.label}>
			{option.label}
		</option>
	{/each}
</select>

<style>
	.xf-curve {
		font: inherit;
		font-size: 7px;
		max-width: 72px;
		padding: 0 2px;
		align-self: center;
		max-height: 28px;
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #23282f);
		color: var(--rb-text-dim, #838990);
	}
</style>
