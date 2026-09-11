<script lang="ts">
	import { ANALYSIS_COLORS, jobProgress } from '$lib/rb/job-progress.svelte';
	import LibraryJobQueuePanel from './LibraryJobQueuePanel.svelte';

	let open = $state(false);
	const ribbon = $derived(jobProgress.ribbon());
</script>

{#if ribbon}
	<button
		type="button"
		class="job-ribbon"
		style={`--job:${ANALYSIS_COLORS[ribbon.kind]}; --pct:${Math.max(0.02, ribbon.progress)}`}
		title={`${ribbon.label} offload. Click to open the job queue.`}
		aria-label={ribbon.label}
		onclick={() => (open = !open)}
	>
		<span class="job-ribbon-fill"></span>
		<span class="job-ribbon-label">{ribbon.label}</span>
	</button>
{/if}
<LibraryJobQueuePanel bind:open />

<style>
	.job-ribbon {
		position: relative;
		display: inline-flex;
		align-items: center;
		height: 16px;
		min-width: 72px;
		border: 0;
		background: #222;
		color: inherit;
		overflow: hidden;
		cursor: pointer;
	}
	.job-ribbon-fill {
		position: absolute;
		inset: 0 auto 0 0;
		width: calc(var(--pct, 0.02) * 100%);
		background: var(--job, #888);
		opacity: 0.45;
	}
	.job-ribbon-label { position: relative; padding: 0 6px; font-size: 11px; }
</style>
