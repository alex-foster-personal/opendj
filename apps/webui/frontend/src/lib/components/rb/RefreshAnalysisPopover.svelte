<script lang="ts">
	/**
	 * The TopBar refresh-analysis hover popover (CHROME-09, CHROME-10, issue
	 * #3886). RefreshAnalysisButton keeps the button, the polling and every
	 * piece of state, and imports this module on the first hover or click, so
	 * the popover markup is not in the /performance initial chunks: it only
	 * exists once the popover opens. Everything shown here arrives as props.
	 */
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import type { AnalysisQueue, IngestConfig, IngestCoverage, RefreshStatus } from '$lib/rb/api-ingest';

	let {
		wrapEl,
		status,
		coverage,
		config,
		queue,
		fetchError,
		clickFeedback
	}: {
		wrapEl: HTMLSpanElement | undefined;
		status: RefreshStatus | null;
		coverage: IngestCoverage | null;
		config: IngestConfig | null;
		queue: AnalysisQueue | null;
		fetchError: string | null;
		clickFeedback: string | null;
	} = $props();

	const pct = $derived.by(() => {
		if (status === null || status.step_total === 0) return 0;
		return Math.min(1, status.step_done / status.step_total);
	});
</script>

<div
	class="pop"
	data-testid="refresh-analysis-pop"
	data-hover-card=""
	use:triggerFloatingAction={{ getTrigger: () => wrapEl ?? null, preferred: 'below', gap: 4 }}
>
	<div class="pop-title">Refresh analysis</div>
	<!-- The latest click's own outcome renders first, whatever the hover
	     fetch or a previous run left behind (Codex P2, PR #3896). -->
	{#if clickFeedback !== null}
		<div class="pop-phase" data-testid="refresh-click-feedback">{clickFeedback}</div>
	{/if}
	{#if fetchError !== null}
		<div class="pop-err">{fetchError}</div>
	{:else}
		{#if coverage !== null}
			<div class="pop-cov" title="tracks with a materialised file missing each artifact; unreachable = broken links, fix via /fix-links">
				missing - analysis {coverage.missing.analysis} · stems {coverage.missing.stems} ·
				vocals {coverage.missing.vocals} · unreachable {coverage.unreachable}
			</div>
		{/if}
		{#if queue !== null}
			<div
				class="pop-queue"
				data-testid="analysis-queue-line"
				title="tracks imported locally with no rekordbox twin; the daemon analyzes these on import so the deck gets tempo, key, energy and auto-cue proposals, and auto is the reconcile loop that drives it. The shipped backend measures no downbeats, so these tracks have no fallback beatgrid"
			>
				local (no rekordbox) - {queue.pending} pending · {queue.analyzed} analyzed ·
				{queue.unreachable} unreachable of {queue.unmapped} · auto
				{queue.auto.enabled ? 'on' : 'off'}
			</div>
		{/if}
		{#if config !== null}
			<div class="pop-steps">
				runs: {config.steps.filter((s) => s.enabled).map((s) => s.id).join(', ') || 'none enabled'}
			</div>
		{/if}
		{#if status !== null && status.phase !== 'idle'}
			<div class="pop-phase">
				{status.phase}{status.current_step !== null ? ` - ${status.current_step}` : ''}
				{#if status.step_total > 0}
					({status.step_done}/{status.step_total})
				{/if}
			</div>
			<div class="bar" title={`${status.step_done}/${status.step_total} items in current step`}>
				<div class="bar-fill" style={`width:${Math.round(pct * 100)}%`}></div>
			</div>
			{#if status.log_tail.length > 0}
				<pre class="pop-log">{status.log_tail.slice(-8).join('\n')}</pre>
			{/if}
		{:else if clickFeedback === null}
			<div class="pop-phase">idle - click to run the enabled steps over every missing track</div>
		{/if}
	{/if}
</div>

<style>
	.pop {
		position: fixed;
		z-index: 60;
		width: 340px;
		padding: 8px 10px;
		background: #14171b;
		border: 1px solid #2a2f36;
		border-radius: 4px;
		font-size: 11px;
		color: #c8cfd6;
		box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
	}
	.pop-title {
		font-weight: 600;
		margin-bottom: 4px;
		color: #e8edf2;
	}
	.pop-cov,
	.pop-queue,
	.pop-steps,
	.pop-phase {
		margin-bottom: 4px;
	}
	.pop-err {
		color: #e5484d;
	}
	.bar {
		height: 5px;
		background: #242a31;
		border-radius: 3px;
		overflow: hidden;
		margin-bottom: 5px;
	}
	.bar-fill {
		height: 100%;
		background: #4cc9f0;
		transition: width 400ms linear;
	}
	.pop-log {
		max-height: 110px;
		overflow-y: auto;
		margin: 0;
		padding: 5px 6px;
		background: #0d0f12;
		border-radius: 3px;
		font-size: 10px;
		line-height: 1.35;
		white-space: pre-wrap;
		word-break: break-all;
	}
</style>
