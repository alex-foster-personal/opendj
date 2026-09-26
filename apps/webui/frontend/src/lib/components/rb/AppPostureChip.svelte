<script lang="ts">
	import { readMachinePressure } from '$lib/rb/machine-pressure';
	import { setAppPosture, uiPrefs } from '$lib/rb/prefs.svelte';

	const helperActive = $derived(uiPrefs.gig_helper === 'on' && uiPrefs.app_posture === 'gig');
	const pressureBand = $derived(readMachinePressure()?.band ?? 'unknown');
</script>

<div class="posture-wrap">
	<div class="posture-chip" role="group" aria-label="Resource posture">
		<button
			type="button"
			class="posture-btn"
			aria-pressed={uiPrefs.app_posture === 'prep'}
			title="Resource posture: Prep. Background poll, workers, and prefetch stay at practice levels. Switch to Gig to cap them for a live set. This is not the app-mode chooser."
			onclick={() => setAppPosture('prep')}
		>
			Prep
		</button>
		<button
			type="button"
			class="posture-btn"
			aria-pressed={uiPrefs.app_posture === 'gig'}
			title="Resource posture: Gig. Library poll is 5 minutes, background workers are halved, prefetch is 2 tracks / 24 MiB. This is not the app-mode chooser."
			onclick={() => setAppPosture('gig')}
		>
			Gig
		</button>
	</div>
	{#if helperActive}
		<span
			class="helper-on"
			data-testid="gig-helper-on"
			title={`Gig helper is on: system pressure is monitored (${pressureBand} in PerfMeters) and Gig background caps stay active.`}
			aria-label="Gig helper active"
		>
			helper
		</span>
	{/if}
</div>

<style>
	.posture-wrap {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		margin-left: 6px;
	}
	.posture-chip {
		display: inline-flex;
		gap: 2px;
	}
	.posture-btn {
		font: inherit;
		font-size: 10px;
		line-height: 1;
		padding: 3px 6px;
		border: 1px solid var(--rb-border, #444);
		background: transparent;
		color: var(--rb-text-muted, #aaa);
		cursor: pointer;
	}
	.posture-btn[aria-pressed='true'] {
		border-color: var(--rb-accent, #4af);
		color: var(--rb-text, #fff);
	}
	.helper-on {
		font-size: 9px;
		font-weight: 700;
		color: var(--rb-accent, #4af);
		text-transform: lowercase;
		cursor: help;
	}
</style>
