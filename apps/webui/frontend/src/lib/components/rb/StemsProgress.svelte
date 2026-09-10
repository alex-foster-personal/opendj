<!--
	Aggregate stems-separation progress, for the top of the menu bar.

	Live off `jobs.updated` through the shared jobs store: the engine writes a
	real 0..1 per job and this averages the active ones. Nothing here animates
	on a timer -- a bar that moved on its own would be inventing progress on
	work that is costing money on a rented GPU.

	It renders NOTHING when no stems job is active, so the TopBar keeps its
	space back the rest of the time. Every number carries a title (house rule).

	All arithmetic is in `$lib/rb/stems-jobs.svelte` so the unit suite can
	reach it; this file is the markup.
-->
<script lang="ts">
	import { jobsRefusal } from '$lib/api/capabilities.svelte';
	import { jobsStore, progressPct, toggleJobsDrawer } from '$lib/rb/jobs-store.svelte';
	import { stemsProgress, stemsProgressTitle } from '$lib/rb/stems-jobs.svelte';

	/** Null when the daemon offers jobs; a sentence when it does not. */
	const refusal = $derived(jobsRefusal());
	const state = $derived(stemsProgress(jobsStore.jobs));
	const title = $derived(stemsProgressTitle(state));
	const pct = $derived(progressPct(state.progress));

	// Hold the live subscription for as long as this bar is on screen. The
	// store reference-counts holders, so the drawer opening and closing under
	// us cannot take the bus away and freeze the bar.
	$effect(() => {
		if (refusal !== null) return;
		return jobsStore.attach();
	});
</script>

{#if refusal === null && state.active}
	<button class="stems-progress" title={title} onclick={toggleJobsDrawer} aria-label="Stems separation progress, opens the jobs drawer">
		<span class="stems-label" title={title}>STEMS</span>
		<span
			class="stems-bar"
			role="progressbar"
			aria-valuenow={pct}
			aria-valuemin="0"
			aria-valuemax="100"
			aria-label="Stems separation progress"
			title={title}
		>
			<span class="stems-fill" style={`width: ${pct}%`}></span>
			<span class="stems-pct" title={title}>{pct}%</span>
		</span>
	</button>
{/if}

<style>
	.stems-progress {
		display: flex;
		align-items: center;
		gap: 6px;
		background: none;
		border: none;
		padding: 0 6px;
		cursor: pointer;
		font: inherit;
		color: var(--rb-text-dim);
	}
	.stems-label {
		font-size: 9px;
		letter-spacing: 0.08em;
	}
	/* Same shape as the drawer's own bar, so one surface does not teach a
	   different visual language from the other. */
	.stems-bar {
		position: relative;
		display: block;
		width: 120px;
		height: 10px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		overflow: hidden;
	}
	.stems-fill {
		display: block;
		height: 100%;
		background: var(--rb-accent);
	}
	.stems-pct {
		position: absolute;
		inset: 0;
		text-align: center;
		font-size: 9px;
		line-height: 10px;
		color: var(--rb-text);
		font-variant-numeric: tabular-nums;
	}
</style>
