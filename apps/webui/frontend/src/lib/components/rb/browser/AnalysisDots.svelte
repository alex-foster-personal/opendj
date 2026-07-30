<script lang="ts">
	import {
		ANALYSIS_DOT_SLOTS,
		ANALYSIS_COLORS,
		type AnalysisBadge,
		type AnalysisKind
	} from '$lib/rb/job-progress.svelte';

	let {
		badge = {},
		title = 'analysis coverage'
	}: {
		badge?: AnalysisBadge;
		title?: string;
	} = $props();

	const doneCount = $derived(
		ANALYSIS_DOT_SLOTS.filter((k): k is AnalysisKind => k !== null && badge[k] === true).length
	);
	const totalSlots = ANALYSIS_DOT_SLOTS.filter((k) => k !== null).length;
	const allDone = $derived(doneCount === totalSlots && totalSlots > 0);
</script>

<span
	class="analysis-dots"
	class:all-done={allDone}
	title={`${title}: ${doneCount}/${totalSlots}`}
	aria-label={`${doneCount} of ${totalSlots} analyses done`}
>
	{#each ANALYSIS_DOT_SLOTS as kind, i (i)}
		{#if kind === null}
			<span class="dot empty" aria-hidden="true"></span>
		{:else}
			<span
				class="dot"
				class:on={badge[kind] === true}
				style={`--dot:${ANALYSIS_COLORS[kind]}`}
				data-kind={kind}
				aria-hidden="true"
			></span>
		{/if}
	{/each}
</span>

<style>
	.analysis-dots {
		display: grid;
		grid-template-columns: repeat(3, 4px);
		grid-template-rows: repeat(3, 4px);
		gap: 1px;
		width: 14px;
		height: 14px;
		margin: 0 auto;
		box-sizing: border-box;
	}
	.analysis-dots.all-done {
		outline: 1px solid color-mix(in srgb, var(--rb-text, #ddd) 35%, transparent);
		outline-offset: 1px;
	}
	.dot {
		width: 4px;
		height: 4px;
		border-radius: 0.5px;
		background: color-mix(in srgb, var(--rb-text, #888) 18%, transparent);
	}
	.dot.on {
		background: var(--dot);
	}
	.dot.empty {
		background: transparent;
	}
</style>
