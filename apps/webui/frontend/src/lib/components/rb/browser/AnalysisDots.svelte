<script lang="ts">
	import {
		ANALYSIS_DOT_SLOTS,
		ANALYSIS_COLORS,
		ANALYSIS_ISSUE_COLORS,
		ANALYSIS_LABELS,
		type AnalysisBadge,
		type AnalysisIssues,
		type AnalysisKind
	} from '$lib/rb/job-progress.svelte';

	/** 'coverage' (default) = today's "is it done" funnel dots; 'issues' =
	 * the Err column, colored red/orange only for kinds with a REAL detected
	 * problem - undetected kinds stay off, never a guessed/fabricated state.
	 *
	 * Presentational only (props in, markup out) - no global store, no
	 * fetch: this is what keeps it storied (storybook-stories.test.mjs holds
	 * that line). AnalysisDotsPopover.svelte wraps this with a live hover
	 * popover (jobProgress, order-analysis) and is deliberately NOT storied,
	 * the same split PerfMeters already establishes for anything reaching
	 * for jobProgress. */
	let {
		badge = {},
		issues = {},
		mode = 'coverage',
		title,
		suppressDotTitles = false
	}: {
		badge?: AnalysisBadge;
		issues?: AnalysisIssues;
		mode?: 'coverage' | 'issues';
		title?: string;
		/** Set by AnalysisDotsPopover: its richer per-kind popover supersedes
		 * these native per-dot titles, and showing both at once is the exact
		 * overlap ControlExplainer's own doc comment (pin dd4f0f5ae33f) warns
		 * against - drop the native title there, keep only the container's. */
		suppressDotTitles?: boolean;
	} = $props();

	const doneCount = $derived(
		ANALYSIS_DOT_SLOTS.filter((k): k is AnalysisKind => k !== null && badge[k] === true).length
	);
	const issueCount = $derived(
		ANALYSIS_DOT_SLOTS.filter((k): k is AnalysisKind => k !== null && issues[k] !== undefined).length
	);
	const totalSlots = ANALYSIS_DOT_SLOTS.filter((k) => k !== null).length;
	const allDone = $derived(mode === 'coverage' && doneCount === totalSlots && totalSlots > 0);
	const containerTitle = $derived(
		title !== undefined
			? title
			: mode === 'issues'
				? `data-quality issues: ${issueCount}`
				: `analysis coverage: ${doneCount}/${totalSlots}`
	);

	function dotOn(kind: AnalysisKind): boolean {
		return mode === 'issues' ? issues[kind] !== undefined : badge[kind] === true;
	}

	function dotColor(kind: AnalysisKind): string | undefined {
		if (mode === 'issues') {
			const issue = issues[kind];
			return issue === undefined ? undefined : ANALYSIS_ISSUE_COLORS[issue.severity];
		}
		return ANALYSIS_COLORS[kind];
	}

	function dotTitle(kind: AnalysisKind): string | undefined {
		if (suppressDotTitles) return undefined;
		if (mode === 'issues') return issues[kind]?.detail;
		return `${ANALYSIS_LABELS[kind]} analysis: ${badge[kind] === true ? 'done' : 'not yet analyzed'}`;
	}
</script>

<span
	class="analysis-dots"
	class:all-done={allDone}
	title={containerTitle}
	aria-label={containerTitle}
>
	{#each ANALYSIS_DOT_SLOTS as kind, i (i)}
		{#if kind === null}
			<span class="dot empty" aria-hidden="true"></span>
		{:else}
			<span
				class="dot"
				class:on={dotOn(kind)}
				style={dotOn(kind) ? `--dot:${dotColor(kind)}` : undefined}
				data-kind={kind}
				title={dotTitle(kind)}
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
		/* Skin hook: mono-dev grays the status grid (theme.css). */
		filter: var(--rb-status-dot-filter);
	}
	.dot.empty {
		background: transparent;
	}
</style>
